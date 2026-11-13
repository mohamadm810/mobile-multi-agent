"""Pure-Python, path-restricted patch preparation. No shell or file mutation."""

import os
import re
import stat
import tempfile
from pathlib import Path, PurePosixPath

from .patch_spec import PatchSpecError, validate_patch_spec

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")
MAX_TARGET_BYTES = 2_000_000


def apply_unified_diff(original, unified_diff, path):
    """Apply validated unified-diff hunks with exact context matching."""
    if not isinstance(original, str):
        raise PatchSpecError("Target file must be UTF-8 text.")
    lines = unified_diff.splitlines(keepends=True)
    if len(lines) < 3 or lines[0].rstrip("\r\n") != f"--- a/{path}" or lines[1].rstrip("\r\n") != f"+++ b/{path}":
        raise PatchSpecError("Unified diff headers do not match the target.")
    source = original.splitlines(keepends=True)
    result = []
    source_cursor = 0
    index = 2
    hunk_count = 0

    while index < len(lines):
        header = lines[index].rstrip("\r\n")
        match = HUNK_HEADER.fullmatch(header)
        if not match:
            raise PatchSpecError("Unexpected content outside a unified-diff hunk.")
        old_start = int(match.group(1))
        old_count = int(match.group(2) if match.group(2) is not None else 1)
        new_start = int(match.group(3))
        new_count = int(match.group(4) if match.group(4) is not None else 1)
        if old_count < 0 or new_count < 0:
            raise PatchSpecError("Negative hunk line counts are invalid.")
        if (old_count > 0 and old_start < 1) or (new_count > 0 and new_start < 1):
            raise PatchSpecError("Invalid zero-based hunk coordinates.")
        target_index = old_start if old_count == 0 else old_start - 1
        new_target_index = new_start if new_count == 0 else new_start - 1
        if target_index < source_cursor or target_index > len(source):
            raise PatchSpecError("Hunks overlap or target lines outside the file.")
        result.extend(source[source_cursor:target_index])
        if new_target_index != len(result):
            raise PatchSpecError("New-side hunk coordinates do not match the generated output.")
        source_cursor = target_index
        index += 1
        consumed_old = 0
        produced_new = 0

        while index < len(lines) and not lines[index].startswith("@@ "):
            item = lines[index]
            if item.startswith("\\ No newline at end of file"):
                raise PatchSpecError("No-newline markers are not supported; use newline-terminated UTF-8 files.")
            if not item or item[0] not in {" ", "+", "-"}:
                raise PatchSpecError("Malformed unified-diff hunk line.")
            prefix, content = item[0], item[1:]
            if prefix == " ":
                if source_cursor >= len(source) or source[source_cursor] != content:
                    raise PatchSpecError("Patch context does not match the current file.")
                result.append(source[source_cursor])
                source_cursor += 1
                consumed_old += 1
                produced_new += 1
            elif prefix == "-":
                if source_cursor >= len(source) or source[source_cursor] != content:
                    raise PatchSpecError("Removed line does not match the current file.")
                source_cursor += 1
                consumed_old += 1
            else:
                result.append(content)
                produced_new += 1
            index += 1

        if consumed_old != old_count or produced_new != new_count:
            raise PatchSpecError("Unified-diff hunk line counts do not match its header.")
        hunk_count += 1

    if not hunk_count:
        raise PatchSpecError("Unified diff contains no applicable hunks.")
    result.extend(source[source_cursor:])
    return "".join(result)


def _resolve_existing_target(root, relative_path):
    parts = PurePosixPath(relative_path).parts
    candidate = root
    for part in parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise PatchSpecError("Symbolic-link targets are forbidden.")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        raise PatchSpecError("Patch target does not resolve to an existing file inside the repository.") from None
    if not resolved.is_file() or resolved.stat().st_size > MAX_TARGET_BYTES:
        raise PatchSpecError("Patch target must be a regular text file no larger than 2 MB.")
    return resolved


def prepare_patch_spec(repository_root, proposal):
    """Validate every change and return proposed contents without modifying any file."""
    validated = validate_patch_spec(proposal)
    root = Path(repository_root).resolve(strict=True)
    prepared = {}
    for change in validated["changes"]:
        target = _resolve_existing_target(root, change["path"])
        try:
            original_bytes = target.read_bytes()
            original = original_bytes.decode("utf-8")
        except (OSError, UnicodeError):
            raise PatchSpecError("Patch target cannot be read as UTF-8 text.") from None
        prepared[change["path"]] = apply_unified_diff(
            original, change["unified_diff"], change["path"]
        )
    return prepared


def apply_patch_spec(repository_root, proposal, *, authorization_phrase):
    """Apply a bounded multi-file patch with staged writes and best-effort rollback."""
    if authorization_phrase != "APPLY_TO_BRANCH":
        raise PatchSpecError("Explicit APPLY_TO_BRANCH authorization is required.")
    prepared = prepare_patch_spec(repository_root, proposal)
    root = Path(repository_root).resolve(strict=True)
    staged = []
    committed = []
    try:
        # Stage every replacement before touching any original file.
        for relative_path, updated in prepared.items():
            target = _resolve_existing_target(root, relative_path)
            original_bytes = target.read_bytes()
            original_mode = stat.S_IMODE(target.stat().st_mode)
            try:
                current_text = original_bytes.decode("utf-8")
            except UnicodeError:
                raise PatchSpecError("Patch target changed to non-UTF-8 content during preparation.") from None
            if apply_unified_diff(current_text, next(
                item["unified_diff"] for item in validate_patch_spec(proposal)["changes"]
                if item["path"] == relative_path
            ), relative_path) != updated:
                raise PatchSpecError("Patch preparation changed during transaction.")
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=target.parent, prefix=f".{target.name}.",
                suffix=".patch-tmp", delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(updated.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(original_mode)
            staged.append({
                "path": relative_path, "target": target, "temporary": temporary,
                "original": original_bytes, "mode": original_mode,
            })

        # Detect concurrent edits or path substitution immediately before commit.
        for item in staged:
            target = _resolve_existing_target(root, item["path"])
            if target != item["target"] or target.read_bytes() != item["original"]:
                raise PatchSpecError("Target changed after validation; no patch was committed.")

        for item in staged:
            os.replace(item["temporary"], item["target"])
            committed.append(item)

        return [item["path"] for item in staged]
    except Exception as exc:
        rollback_errors = []
        for item in reversed(committed):
            restore_path = None
            try:
                target = _resolve_existing_target(root, item["path"])
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=target.parent, prefix=f".{target.name}.",
                    suffix=".rollback-tmp", delete=False
                ) as handle:
                    restore_path = Path(handle.name)
                    handle.write(item["original"])
                    handle.flush()
                    os.fsync(handle.fileno())
                restore_path.chmod(item["mode"])
                os.replace(restore_path, target)
                restore_path = None
            except Exception as rollback_exc:
                rollback_errors.append(type(rollback_exc).__name__)
            finally:
                if restore_path is not None:
                    restore_path.unlink(missing_ok=True)
        if rollback_errors:
            raise PatchSpecError(
                "Patch transaction failed and rollback was incomplete; inspect repository state immediately."
            ) from None
        if isinstance(exc, PatchSpecError):
            raise
        raise PatchSpecError(
            f"Patch transaction failed; previously replaced files were restored ({type(exc).__name__})."
        ) from None
    finally:
        for item in staged:
            item["temporary"].unlink(missing_ok=True)
