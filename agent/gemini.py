import http.client
import json
import os
import random
import time
import urllib.error
import urllib.request

from .config import (
    FALLBACK_MODELS,
    MAX_OUTPUT_TOKENS,
    MODEL,
    REQUEST_TIMEOUT_SECONDS,
    API_URL_TEMPLATE,
)


class AgentError(RuntimeError):
    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def reset_run_tracking():
    """Reset per-cycle metadata while preserving fallback selection within a cycle."""
    global ACTIVE_MODEL
    ACTIVE_MODEL = MODEL
    USED_MODELS.clear()
    ATTEMPTED_MODELS.clear()


RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS_PER_MODEL = 3
USED_MODELS = []
ATTEMPTED_MODELS = []
ACTIVE_MODEL = MODEL


def _extract_text(data):
    candidates = data.get("candidates") if isinstance(data, dict) else None
    if not isinstance(candidates, list) or not candidates:
        raise AgentError("Gemini returned no candidates.", retryable=True)
    candidate = candidates[0]
    if not isinstance(candidate, dict):
        raise AgentError("Gemini returned a malformed candidate.", retryable=True)
    content = candidate.get("content", {})
    parts = content.get("parts", []) if isinstance(content, dict) else []
    if not isinstance(parts, list):
        raise AgentError("Gemini returned malformed content parts.", retryable=True)
    chunks = [
        part.get("text", "")
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    result = "\n".join(chunk for chunk in chunks if chunk.strip()).strip()
    if not result:
        raise AgentError("Gemini returned an empty or non-text response.", retryable=True)
    return result


def _retry_delay(error, attempt):
    """Return a jittered delay; None means Retry-After exceeds the safe wait budget."""
    retry_after = error.headers.get("Retry-After") if error is not None and error.headers else None
    if retry_after:
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            delay = None
        if delay is not None and delay >= 0:
            return delay if delay <= 30.0 else None
    base = min(20.0, 2.0 ** max(0, attempt - 1))
    return random.uniform(0.0, base)


def _generate_with_model(model, payload, key):
    req = urllib.request.Request(
        API_URL_TEMPLATE.format(model=model),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": key,
        },
        method="POST",
    )
    for attempt in range(MAX_ATTEMPTS_PER_MODEL):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise AgentError("Gemini response exceeded the 2 MB safety limit.")
                data = json.loads(raw.decode("utf-8"))
            return _extract_text(data)
        except urllib.error.HTTPError as exc:
            if exc.code in RETRYABLE_HTTP_CODES and attempt < MAX_ATTEMPTS_PER_MODEL - 1:
                delay = _retry_delay(exc, attempt + 1)
                if delay is not None:
                    exc.close()
                    time.sleep(delay)
                    continue
            detail = exc.read(1500).decode("utf-8", errors="replace")
            if key:
                detail = detail.replace(key, "[REDACTED]")
            raise AgentError(
                f"Gemini API HTTP {exc.code} on {model}: {detail}",
                retryable=exc.code in RETRYABLE_HTTP_CODES,
            ) from None
        except AgentError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            if attempt < MAX_ATTEMPTS_PER_MODEL - 1:
                time.sleep(_retry_delay(None, attempt + 1))
                continue
            raise AgentError(
                f"Gemini transport failed on {model} after {MAX_ATTEMPTS_PER_MODEL} attempts: "
                f"{type(exc).__name__}: {exc}",
                retryable=True,
            ) from None
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise AgentError(
                f"Gemini returned an invalid response on {model}: {type(exc).__name__}", retryable=True
            ) from None
    raise AgentError(f"Gemini API request failed on {model} after retries.")


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

    global ACTIVE_MODEL
    models = list(dict.fromkeys([ACTIVE_MODEL, *FALLBACK_MODELS]))
    for index, model in enumerate(models):
        if model not in ATTEMPTED_MODELS:
            ATTEMPTED_MODELS.append(model)
        try:
            result = _generate_with_model(model, payload, key)
            ACTIVE_MODEL = model
            if model not in USED_MODELS:
                USED_MODELS.append(model)
            return result
        except AgentError as exc:
            if index < len(models) - 1 and exc.retryable:
                continue
            raise
    raise AgentError("All configured Gemini models failed.")
