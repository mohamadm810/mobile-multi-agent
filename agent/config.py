import os
import re

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
FALLBACK_MODELS = [
    item.strip()
    for item in os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.8-flash").split(",")
    if item.strip()
]
for model_name in [MODEL, *FALLBACK_MODELS]:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", model_name):
        raise ValueError("Invalid Gemini model name.")
API_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
API_URL = API_URL_TEMPLATE.format(model=MODEL)


def _positive_int_env(name, default):
    """Read a positive integer environment setting with a clear configuration error."""
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive integer.") from None
    if value < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return value


MAX_OUTPUT_TOKENS = _positive_int_env("MAX_OUTPUT_TOKENS", 3000)
REQUEST_TIMEOUT_SECONDS = _positive_int_env("GEMINI_TIMEOUT_SECONDS", 90)
MAX_REQUEST_CHARS = _positive_int_env("MAX_REQUEST_CHARS", 12000)
