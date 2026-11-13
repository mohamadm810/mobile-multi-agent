"""Bounded retry and durable stage-checkpoint primitives for agent runs."""

import json
import os
import tempfile
import time
from pathlib import Path

CHECKPOINT_VERSION = 1
MAX_CHECKPOINT_BYTES = 250_000
MAX_STAGE_OUTPUT_CHARS = 25_000
MAX_STAGES = 32


def call_with_retries(operation, *, max_attempts=2, sleep=time.sleep, is_retryable=None):
    """Retry only explicitly retryable failures; never retry arbitrary exceptions."""
    if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 3:
        raise ValueError("max_attempts must be between 1 and 3.")
    retryable = is_retryable or (lambda exc: bool(getattr(exc, "retryable", False)))
    for attempt in range(max_attempts):
        try:
            return operation()
        except Exception as exc:
            if attempt + 1 >= max_attempts or not retryable(exc):
                raise
            sleep(min(5.0, 2.0 ** attempt))


class CheckpointStore:
    """Persist bounded stage outputs and resume only for the same request and source."""

    def __init__(self, path, request_fingerprint, source_fingerprint):
        self.path = Path(path)
        self.request_fingerprint = str(request_fingerprint)
        self.source_fingerprint = str(source_fingerprint)
        self.outputs = {}
        self.metadata = {}
        self._load()

    def _load(self):
        try:
            if not self.path.is_file() or self.path.stat().st_size > MAX_CHECKPOINT_BYTES:
                return
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        if not isinstance(data, dict) or data.get("version") != CHECKPOINT_VERSION:
            return
        if (data.get("request_fingerprint") != self.request_fingerprint
                or data.get("source_fingerprint") != self.source_fingerprint):
            return
        outputs = data.get("outputs")
        metadata = data.get("metadata", {})
        if not isinstance(outputs, dict) or len(outputs) > MAX_STAGES or not isinstance(metadata, dict):
            return
        clean = {}
        total = 0
        for name, output in outputs.items():
            if (not isinstance(name, str) or not 1 <= len(name) <= 100
                    or not isinstance(output, str) or len(output) > MAX_STAGE_OUTPUT_CHARS):
                return
            total += len(name) + len(output)
            if total > MAX_CHECKPOINT_BYTES:
                return
            clean[name] = output
        self.outputs = clean
        self.metadata = metadata

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CHECKPOINT_VERSION,
            "request_fingerprint": self.request_fingerprint,
            "source_fingerprint": self.source_fingerprint,
            "outputs": self.outputs,
            "metadata": self.metadata,
        }
        encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if len(encoded.encode("utf-8")) > MAX_CHECKPOINT_BYTES:
            raise ValueError("Checkpoint exceeds the configured size limit.")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=f".{self.path.name}.", suffix=".tmp", delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def record_stage(self, name, output):
        if not isinstance(name, str) or not 1 <= len(name) <= 100:
            raise ValueError("Invalid checkpoint stage name.")
        if not isinstance(output, str) or len(output) > MAX_STAGE_OUTPUT_CHARS:
            raise ValueError("Invalid or oversized checkpoint stage output.")
        if name not in self.outputs and len(self.outputs) >= MAX_STAGES:
            raise ValueError("Checkpoint stage limit reached.")
        self.outputs[name] = output
        self._save()

    def get_stage(self, name):
        return self.outputs.get(name)

    def set_metadata(self, name, value):
        if not isinstance(name, str) or not 1 <= len(name) <= 100:
            raise ValueError("Invalid checkpoint metadata key.")
        self.metadata[name] = value
        self._save()

    def get_metadata(self, name, default=None):
        return self.metadata.get(name, default)

    def clear(self):
        self.outputs = {}
        self.metadata = {}
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass
