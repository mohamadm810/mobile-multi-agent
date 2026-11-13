import http.client
import json
import os
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.request
from urllib.parse import urlparse

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
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
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


def _openai_endpoint():
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or "https://api.openai.com/v1"
    base_url = base_url.rstrip("/")
    parsed = urlparse(base_url)
    if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise AgentError("OPENAI_BASE_URL must be an HTTPS base URL without credentials, query, or fragment.")
    model = os.environ.get("OPENAI_MODEL", "").strip() or "gpt-4.1-mini"
    if not model or len(model) > 120 or any(ord(char) < 32 for char in model):
        raise AgentError("OPENAI_MODEL is invalid.")
    return f"{base_url}/chat/completions", model


def _extract_openai_text(data):
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise AgentError("OpenAI-compatible provider returned no valid choices.", retryable=True)
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        chunks = [part.get("text", "") for part in content
                  if isinstance(part, dict) and isinstance(part.get("text"), str)]
        result = "\\n".join(chunk for chunk in chunks if chunk.strip()).strip()
        if result:
            return result
    raise AgentError("OpenAI-compatible provider returned empty or non-text content.", retryable=True)


def _generate_with_openai(role, prompt, key):
    endpoint, model = _openai_endpoint()
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": role},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": MAX_OUTPUT_TOKENS,
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    for attempt in range(MAX_ATTEMPTS_PER_MODEL):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise AgentError("OpenAI-compatible response exceeded the 2 MB safety limit.")
                data = json.loads(raw.decode("utf-8"))
            return model, _extract_openai_text(data)
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
                f"OpenAI-compatible provider HTTP {exc.code} on {model}: {detail}",
                retryable=exc.code in RETRYABLE_HTTP_CODES,
            ) from None
        except AgentError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            if attempt < MAX_ATTEMPTS_PER_MODEL - 1:
                time.sleep(_retry_delay(None, attempt + 1))
                continue
            raise AgentError(
                f"OpenAI-compatible transport failed on {model}: {type(exc).__name__}",
                retryable=True,
            ) from None
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise AgentError(
                f"OpenAI-compatible provider returned an invalid response: {type(exc).__name__}",
                retryable=True,
            ) from None
    raise AgentError("OpenAI-compatible provider request failed after retries.", retryable=True)


def generate(role: str, prompt: str) -> str:
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not gemini_key and not openai_key:
        raise AgentError("Missing GEMINI_API_KEY or OPENAI_API_KEY provider secret.")
    if not isinstance(role, str) or not role.strip() or not isinstance(prompt, str) or not prompt.strip():
        raise AgentError("Role and prompt must not be empty.")

    payload = {
        "system_instruction": {"parts": [{"text": role}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": MAX_OUTPUT_TOKENS},
    }

    global ACTIVE_MODEL
    last_error = None
    if gemini_key:
        models = list(dict.fromkeys([ACTIVE_MODEL, *FALLBACK_MODELS]))
        for model in models:
            if model not in ATTEMPTED_MODELS:
                ATTEMPTED_MODELS.append(model)
            try:
                result = _generate_with_model(model, payload, gemini_key)
                ACTIVE_MODEL = model
                if model not in USED_MODELS:
                    USED_MODELS.append(model)
                return result
            except AgentError as exc:
                last_error = exc
                if not exc.retryable:
                    raise
        # A separately configured provider is a real fallback only for transient failures.
    if openai_key and (last_error is None or last_error.retryable):
        endpoint, model = _openai_endpoint()
        provider_label = f"openai:{model}"
        if provider_label not in ATTEMPTED_MODELS:
            ATTEMPTED_MODELS.append(provider_label)
        try:
            used_model, result = _generate_with_openai(role, prompt, openai_key)
            if provider_label not in USED_MODELS:
                USED_MODELS.append(provider_label)
            return result
        except AgentError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise AgentError("No configured model provider could be used.")
