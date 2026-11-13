import os
import re

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
FALLBACK_MODELS = [
    item.strip()
    for item in os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash-lite").split(",")
    if item.strip()
]
for model_name in [MODEL, *FALLBACK_MODELS]:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", model_name):
        raise ValueError("Invalid Gemini model name.")
API_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
API_URL = API_URL_TEMPLATE.format(model=MODEL)
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "3000"))
REQUEST_TIMEOUT_SECONDS = int(os.getenv("GEMINI_TIMEOUT_SECONDS", "90"))
MAX_REQUEST_CHARS = int(os.getenv("MAX_REQUEST_CHARS", "12000"))
