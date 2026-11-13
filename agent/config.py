import os
import re

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", MODEL):
    raise ValueError("Invalid GEMINI_MODEL value.")
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "3000"))
REQUEST_TIMEOUT_SECONDS = int(os.getenv("GEMINI_TIMEOUT_SECONDS", "90"))
MAX_REQUEST_CHARS = int(os.getenv("MAX_REQUEST_CHARS", "12000"))
