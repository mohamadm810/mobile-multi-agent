import json
import os
import time
import urllib.error
import urllib.request

from .config import API_URL, MAX_OUTPUT_TOKENS, REQUEST_TIMEOUT_SECONDS


class AgentError(RuntimeError):
    pass


RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4


def _extract_text(data):
    candidates = data.get("candidates") if isinstance(data, dict) else None
    if not isinstance(candidates, list) or not candidates:
        raise AgentError("Gemini returned no candidates.")
    content = candidates[0].get("content", {})
    parts = content.get("parts", []) if isinstance(content, dict) else []
    chunks = [
        part.get("text", "")
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    result = "\n".join(chunk for chunk in chunks if chunk.strip()).strip()
    if not result:
        raise AgentError("Gemini returned an empty or non-text response.")
    return result


def _retry_delay(error, attempt):
    retry_after = error.headers.get("Retry-After") if error.headers else None
    if retry_after:
        try:
            return min(30.0, max(1.0, float(retry_after)))
        except (TypeError, ValueError):
            pass
    return min(30.0, 2.0 ** attempt)


def generate(role: str, prompt: str) -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise AgentError("Missing GEMINI_API_KEY repository secret.")
    if not role.strip() or not prompt.strip():
        raise AgentError("Role and prompt must not be empty.")

    payload = {
        "system_instruction": {"parts": [{"text": role}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": MAX_OUTPUT_TOKENS,
        },
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": key,
        },
        method="POST",
    )

    for attempt in range(MAX_ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise AgentError("Gemini response exceeded the 2 MB safety limit.")
                data = json.loads(raw.decode("utf-8"))
            return _extract_text(data)
        except urllib.error.HTTPError as exc:
            if exc.code in RETRYABLE_HTTP_CODES and attempt < MAX_ATTEMPTS - 1:
                delay = _retry_delay(exc, attempt + 1)
                exc.close()
                time.sleep(delay)
                continue
            # Do not include request headers or API key in error output.
            detail = exc.read(1500).decode("utf-8", errors="replace")
            raise AgentError(f"Gemini API HTTP {exc.code}: {detail}") from None
        except AgentError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AgentError(f"Gemini API request failed: {type(exc).__name__}: {exc}") from None

    raise AgentError("Gemini API request failed after retry attempts.")
