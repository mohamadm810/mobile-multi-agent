"""Strict schema and path validation for machine-generated patch proposals."""

import json
from pathlib import PurePosixPath

MAX_CHANGES = 10
MAX_DIFF_CHARS = 50_000
ALLOWED_PREFIXES = ("agent/", "tests/")


class PatchSpecError(ValueError):
    pass


def validate_target_path(path):
    if not isinstance(path, str) or not path or len(path) > 240:
        raise PatchSpecError("Patch target path is missing or too long.")
    if "\\" in path or "\x00" in path or path.startswith("/"):
        raise PatchSpecError("Absolute paths, backslashes, and NUL bytes are forbidden.")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise PatchSpecError("Patch target contains an unsafe path component.")
    if not any(path.startswith(prefix) for prefix in ALLOWED_PREFIXES):
        raise PatchSpecError("Patch target must be inside agent/ or tests/.")
    if PurePosixPath(path).as_posix() != path:
        raise PatchSpecError("Patch target path is not canonical.")
    return path


def validate_unified_diff_header(path, unified_diff):
    validate_target_path(path)
    if not isinstance(unified_diff, str) or not unified_diff or len(unified_diff) > MAX_DIFF_CHARS:
        raise PatchSpecError("Unified diff is empty or exceeds the size limit.")
    lines = unified_diff.splitlines()
    if len(lines) < 3:
        raise PatchSpecError("Unified diff must include file headers and at least one hunk.")
    if lines[0] != f"--- a/{path}" or lines[1] != f"+++ b/{path}":
        raise PatchSpecError("Unified diff headers must exactly match the validated target path.")
    forbidden = (
        "diff --git ", "index ", "new file mode ", "deleted file mode ",
        "old mode ", "new mode ", "rename from ", "rename to ",
        "copy from ", "copy to ", "GIT binary patch", "Binary files ",
    )
    if any(line.startswith(forbidden) for line in lines):
        raise PatchSpecError("Git metadata, file creation/deletion, renames, and binary patches are forbidden.")
    if not any(line.startswith("@@ ") for line in lines[2:]):
        raise PatchSpecError("Unified diff contains no hunks.")
    return unified_diff


def validate_patch_spec(payload):
    """Validate and normalize a JSON object or JSON string containing bounded file changes."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise PatchSpecError("Patch proposal is not valid JSON.") from exc
    if not isinstance(payload, dict) or set(payload) != {"version", "changes"}:
        raise PatchSpecError("Patch proposal must contain only version and changes.")
    if type(payload["version"]) is not int or payload["version"] != 1:
        raise PatchSpecError("Unsupported patch proposal version.")
    changes = payload["changes"]
    if not isinstance(changes, list) or not 1 <= len(changes) <= MAX_CHANGES:
        raise PatchSpecError(f"Patch proposal must contain between 1 and {MAX_CHANGES} changes.")
    normalized, seen, total_chars = [], set(), 0
    for change in changes:
        if not isinstance(change, dict) or set(change) != {"path", "unified_diff"}:
            raise PatchSpecError("Each change must contain only path and unified_diff.")
        path = validate_target_path(change["path"])
        if path in seen:
            raise PatchSpecError("A file may appear only once in a patch proposal.")
        seen.add(path)
        diff = validate_unified_diff_header(path, change["unified_diff"])
        total_chars += len(diff)
        if total_chars > 120_000:
            raise PatchSpecError("Combined patch proposal exceeds the 120 KB size limit.")
        normalized.append({"path": path, "unified_diff": diff})
    return {"version": 1, "changes": normalized}
