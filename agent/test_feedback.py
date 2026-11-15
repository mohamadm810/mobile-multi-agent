"""Bounded in-job unittest result for agent prompts.

The file records counts and test identifiers only. Tracebacks, stdout, and
provider credentials are never persisted.
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

STATUS_FILENAME = "TEST_STATUS.json"
SCHEMA = 1
MAX_STATUS_BYTES = 16_000
MAX_FAILED_TESTS = 20
MAX_DURATION_SECONDS = 600
RUNNER = "unittest-subprocess"
_CHILD = r"""
import json, os, sys, time, unittest
from pathlib import Path
root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
started = time.perf_counter()
suite = unittest.defaultTestLoader.discover(str(root / "tests"), pattern="test_*.py")
with open(os.devnull, "w", encoding="utf-8") as sink:
    result = unittest.TextTestRunner(stream=sink, verbosity=0).run(suite)

def names(items):
    found = []
    for test, _trace in items:
        method = getattr(test, "_testMethodName", "")
        kind = type(test)
        found.append(f"{kind.__module__}.{kind.__qualname__}.{method}")
    return found

print(json.dumps({
    "tests_run": result.testsRun,
    "failures": len(result.failures),
    "errors": len(result.errors),
    "skipped": len(result.skipped),
    "failed_tests": names(list(result.failures) + list(result.errors)),
    "duration_seconds": round(time.perf_counter() - started, 3),
}))
sys.exit(0 if result.wasSuccessful() else 1)
"""


def _atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _unavailable(reason):
    return {
        "conclusion": "unavailable",
        "tests_run": None,
        "failures": None,
        "errors": None,
        "skipped": None,
        "failed_tests": [],
        "duration_seconds": None,
        "head_sha": None,
        "reason": reason,
    }


def _clean_identifier(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 180:
        return None
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.")
    if value[0].isdigit() or any(char not in allowed for char in value):
        return None
    return value


def _normalize_counts(payload):
    counts = {}
    for key in ("tests_run", "failures", "errors", "skipped"):
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 100_000:
            return None
        counts[key] = value
    if counts["failures"] + counts["errors"] > counts["tests_run"]:
        return None
    if counts["skipped"] > counts["tests_run"]:
        return None
    duration = payload.get("duration_seconds")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        return None
    if duration < 0 or duration > MAX_DURATION_SECONDS:
        return None
    failed = payload.get("failed_tests")
    if not isinstance(failed, list) or len(failed) > MAX_FAILED_TESTS:
        return None
    clean_failed = []
    for item in failed:
        name = _clean_identifier(item)
        if name is None:
            return None
        clean_failed.append(name)
    if len(clean_failed) > counts["failures"] + counts["errors"]:
        return None
    return counts, round(float(duration), 3), clean_failed


def collect_unittest_result(root, *, timeout_seconds=120, runner=None):
    """Run the fixed unittest child. runner is injectable for tests."""
    root = Path(root)
    if runner is None:
        safe_env = {
            "PATH": os.environ.get("PATH", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "HOME": os.environ.get("HOME", "/tmp"),
        }
        try:
            completed = subprocess.run(
                [sys.executable, "-c", _CHILD, str(root)],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=safe_env,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return _unavailable("timeout")
        except OSError:
            return _unavailable("runner_failed")
        stdout, return_code = completed.stdout, completed.returncode
    else:
        try:
            stdout, return_code = runner(root)
        except subprocess.TimeoutExpired:
            return _unavailable("timeout")
        except OSError:
            return _unavailable("runner_failed")
    line = next((item for item in reversed(stdout.splitlines()) if item.strip().startswith("{")), "")
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return _unavailable("invalid_result")
    if not isinstance(payload, dict):
        return _unavailable("invalid_result")
    normalized = _normalize_counts(payload)
    if normalized is None:
        return _unavailable("invalid_result")
    counts, duration, failed = normalized
    if return_code == 0 and counts["failures"] == 0 and counts["errors"] == 0:
        conclusion = "success"
    elif return_code == 1 and counts["failures"] + counts["errors"] > 0:
        conclusion = "failure"
    else:
        return _unavailable("inconsistent_result")
    return {
        "conclusion": conclusion,
        "tests_run": counts["tests_run"],
        "failures": counts["failures"],
        "errors": counts["errors"],
        "skipped": counts["skipped"],
        "failed_tests": failed,
        "duration_seconds": duration,
        "head_sha": None,
        "reason": None,
    }


def write_test_status(workspace, root, *, timeout_seconds=120, runner=None):
    result = collect_unittest_result(root, timeout_seconds=timeout_seconds, runner=runner)
    head_sha = os.environ.get("GITHUB_SHA", "").strip() or "local"
    payload = {
        "schema": SCHEMA,
        "runner": RUNNER,
        "conclusion": result["conclusion"] if result["conclusion"] in {"success", "failure"} else "error",
        "tests_run": result["tests_run"],
        "failures": result["failures"],
        "errors": result["errors"],
        "skipped": result["skipped"],
        "failed_tests": result["failed_tests"],
        "duration_seconds": result["duration_seconds"],
        "head_sha": head_sha,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "reason": result["reason"],
    }
    if payload["conclusion"] == "error":
        payload["tests_run"] = None
        payload["failures"] = None
        payload["errors"] = None
        payload["skipped"] = None
        payload["failed_tests"] = []
        payload["duration_seconds"] = None
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    _atomic_write(Path(workspace) / STATUS_FILENAME, encoded)
    return payload


def expected_head_sha():
    return os.environ.get("GITHUB_SHA", "").strip() or "local"


def load_test_feedback(path, *, expected_sha=None):
    """Return prompt-safe feedback. Stale, oversized, or malformed files are unavailable."""
    expected = expected_head_sha() if expected_sha is None else expected_sha
    file_path = Path(path)
    try:
        if not file_path.is_file() or file_path.stat().st_size > MAX_STATUS_BYTES:
            return _unavailable("missing" if not file_path.is_file() else "oversized")
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _unavailable("malformed")
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or data.get("runner") != RUNNER:
        return _unavailable("schema")
    head_sha = data.get("head_sha")
    if not isinstance(head_sha, str) or head_sha != expected:
        return _unavailable("stale_sha")
    conclusion = data.get("conclusion")
    if conclusion == "error":
        reason = data.get("reason")
        if not isinstance(reason, str) or not _clean_identifier(reason):
            reason = "invalid_result"
        status = _unavailable(reason)
        status["head_sha"] = head_sha
        status["conclusion"] = "error"
        return status
    if conclusion not in {"success", "failure"}:
        return _unavailable("schema")
    normalized = _normalize_counts(data)
    if normalized is None:
        return _unavailable("schema")
    counts, duration, failed = normalized
    if conclusion == "success" and (counts["failures"] or counts["errors"]):
        return _unavailable("inconsistent_result")
    if conclusion == "failure" and counts["failures"] + counts["errors"] == 0:
        return _unavailable("inconsistent_result")
    return {
        "conclusion": conclusion,
        "tests_run": counts["tests_run"],
        "failures": counts["failures"],
        "errors": counts["errors"],
        "skipped": counts["skipped"],
        "failed_tests": failed,
        "duration_seconds": duration,
        "head_sha": head_sha,
        "reason": None,
    }


def format_test_feedback(status):
    """One factual block. It never claims the isolated Tests workflow ran."""
    conclusion = status.get("conclusion", "unavailable")
    if conclusion not in {"success", "failure"}:
        reason = status.get("reason") or "missing"
        return (
            "IN-JOB UNITTEST RESULT: unavailable"
            f" ({reason}). This is not the isolated GitHub Tests workflow. "
            "Do not claim that tests passed or failed."
        )
    failed = status.get("failed_tests") or []
    failed_text = ", ".join(failed) if failed else "none"
    return (
        "IN-JOB UNITTEST RESULT (this checkout, before prompts; not the isolated "
        "Tests workflow, not a live API test, and not proof a patch was applied): "
        f"conclusion={conclusion}; tests_run={status['tests_run']}; "
        f"failures={status['failures']}; errors={status['errors']}; "
        f"skipped={status['skipped']}; failed_tests={failed_text}. "
        "Repeat this result exactly; do not invent another."
    )


def main():
    root = Path(__file__).resolve().parents[1]
    write_test_status(root / "workspace", root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
