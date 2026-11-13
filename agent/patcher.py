"""Pure-Python, path-restricted unified-diff application. No shell or external patch tool."""

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
            raise PatchSpecError("New-side hunk coordinates do not match the generated output.");
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


def apply_patch_spec(repository_root, proposal):
    """Validate every change first, then atomically replace each affected existing file."""
    validated = validate_patch_spec(proposal)
    root = Path(repository_root).resolve(strict=True)
    updates = []
    for change in validated["changes"]:
        target = _resolve_existing_target(root, change["path"])
        try:
            original_bytes = target.read_bytes()
            original = original_bytes.decode("utf-8")
        except (OSError, UnicodeError):
            raise PatchSpecError("Patch target cannot be read as UTF-8 text.") from None
        updated = apply_unified_diff(original, change["unified_diff"], change["path"])
        updates.append((target, updated, stat.S_IMODE(target.stat().st_mode)))

    temporary_files = []
    try:
        for target, updated, mode in updates:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="", dir=target.parent,
                prefix=f".{target.name}.", suffix=".patch-tmp", delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
            temporary_files.append((temporary, target))
            temporary.chmod(mode)
        for temporary, target in temporary_files:
            os.replace(temporary, target)
        return [change["path"] for change in validated["changes"]]
    finally:
        for temporary, _ in temporary_files:
            temporary.unlink(missing_ok=True)
