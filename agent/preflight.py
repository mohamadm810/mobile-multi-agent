"""Zero-cost readiness checks that run before any model/API request."""

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from .config import MAX_REQUEST_CHARS

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = (
    "agent/run.py",
    "agent/gemini.py",
    "agent/resilience.py",
    ".github/workflows/run-agent.yml",
)


def _check_request(root):
    request_path = Path(root) / "workspace" / "REQUEST.md"
    if not request_path.is_file():
        return False, "workspace/REQUEST.md is missing"
    raw = request_path.read_text(encoding="utf-8").strip()
    if not raw:
        return False, "workspace/REQUEST.md is empty"
    if len(raw) > MAX_REQUEST_CHARS:
        return False, f"request exceeds {MAX_REQUEST_CHARS} characters"
    request = re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL).strip()
    if not request:
        return False, "request contains only an HTML comment/template"
    return True, f"request ready ({len(request)} characters)"


def _check_openai_endpoint(environ):
    base_url = environ.get("OPENAI_BASE_URL", "").strip() or "https://api.openai.com/v1"
    parsed = urlparse(base_url.rstrip("/"))
    if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        return False, "OPENAI_BASE_URL must be HTTPS without credentials, query, or fragment"
    model = environ.get("OPENAI_MODEL", "").strip() or "gpt-4.1-mini"
    if not model or len(model) > 120 or any(ord(char) < 32 for char in model):
        return False, "OPENAI_MODEL is invalid"
    return True, f"OpenAI-compatible endpoint configured (model: {model})"


def run_preflight(root=ROOT, environ=None):
    """Return a safe readiness report; never return or print secret values."""
    environ = os.environ if environ is None else environ
    checks = []

    checks.append({
        "name": "python_runtime",
        "ok": sys.version_info >= (3, 10),
        "detail": f"Python {sys.version_info.major}.{sys.version_info.minor}",
    })

    for relative in REQUIRED_FILES:
        exists = (Path(root) / relative).is_file()
        checks.append({
            "name": f"required_file:{relative}",
            "ok": exists,
            "detail": "present" if exists else "missing",
        })

    try:
        request_ok, request_detail = _check_request(root)
    except (OSError, UnicodeError) as exc:
        request_ok, request_detail = False, f"cannot read request ({type(exc).__name__})"
    checks.append({"name": "project_request", "ok": request_ok, "detail": request_detail})

    gemini_key = bool(environ.get("GEMINI_API_KEY", "").strip())
    openai_key = bool(environ.get("OPENAI_API_KEY", "").strip())
    checks.append({
        "name": "provider_credentials",
        "ok": gemini_key or openai_key,
        "detail": (
            "Gemini primary and OpenAI-compatible fallback configured" if gemini_key and openai_key
            else "Gemini provider configured" if gemini_key
            else "OpenAI-compatible provider configured as the only available provider" if openai_key
            else "no provider secret configured"
        ),
    })

    if openai_key:
        endpoint_ok, endpoint_detail = _check_openai_endpoint(environ)
        checks.append({"name": "openai_endpoint", "ok": endpoint_ok, "detail": endpoint_detail})

    workspace = Path(root) / "workspace"
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=workspace, prefix=".preflight-", delete=True
        ) as probe:
            probe.write("ok")
            probe.flush()
        writable, write_detail = True, "workspace write probe succeeded"
    except OSError as exc:
        writable, write_detail = False, f"workspace is not writable ({type(exc).__name__})"
    checks.append({"name": "workspace_write", "ok": writable, "detail": write_detail})

    return {
        "status": "ready" if all(item["ok"] for item in checks) else "blocked",
        "checks": checks,
        "blocking_checks": [item["name"] for item in checks if not item["ok"]],
        "model_requests_made": 0,
    }


def main():
    result = run_preflight()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "ready" else 1)


if __name__ == "__main__":
    main()
