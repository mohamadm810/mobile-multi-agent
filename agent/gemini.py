import json
import os
import urllib.error
import urllib.request

from .config import API_URL, MAX_OUTPUT_TOKENS, REQUEST_TIMEOUT_SECONDS


class AgentError(RuntimeError):
    pass


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
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise AgentError("Gemini response exceeded the 2 MB safety limit.")
            data = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Do not include request headers or API key in error output.
        detail = exc.read(1500).decode("utf-8", errors="replace")
        raise AgentError(f"Gemini API HTTP {exc.code}: {detail}") from None
    except AgentError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AgentError(f"Gemini API request failed: {type(exc).__name__}: {exc}") from None

    return _extract_text(data)
