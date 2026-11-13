"""Bounded retries, circuit breaking, and integrity-checked stage checkpoints."""

import hashlib
import json
import os
import random
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

CHECKPOINT_VERSION = 2
MAX_CHECKPOINT_BYTES = 250_000
MAX_STAGE_OUTPUT_CHARS = 25_000
MAX_STAGES = 32


class CircuitOpenError(RuntimeError):
    """Raised when a dependency is temporarily blocked by its circuit breaker."""


class CircuitBreaker:
    """Small in-process circuit breaker with a bounded cooldown."""

    def __init__(self, *, failure_threshold=3, reset_timeout=30.0, clock=time.monotonic):
        if not isinstance(failure_threshold, int) or not 1 <= failure_threshold <= 10:
            raise ValueError("failure_threshold must be between 1 and 10.")
        if not isinstance(reset_timeout, (int, float)) or not 1 <= reset_timeout <= 600:
            raise ValueError("reset_timeout must be between 1 and 600 seconds.")
        self.failure_threshold = failure_threshold
        self.reset_timeout = float(reset_timeout)
        self.clock = clock
        self.failures = 0
        self.opened_at = None
        self.half_open_probe = False

    def before_call(self):
        if self.opened_at is None:
            return
        if self.clock() - self.opened_at < self.reset_timeout:
            raise CircuitOpenError("Dependency circuit is open; recovery cooldown is active.")
        if self.half_open_probe:
            raise CircuitOpenError("Dependency circuit is waiting for its half-open probe.")
        self.half_open_probe = True

    def record_success(self):
        self.failures = 0
        self.opened_at = None
        self.half_open_probe = False

    def record_failure(self):
        self.half_open_probe = False
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.opened_at = self.clock()


def call_with_retries(
    operation, *, max_attempts=2, sleep=time.sleep, is_retryable=None,
    retry_after=None, jitter=random.uniform, max_elapsed_seconds=90.0,
    max_delay_seconds=20.0, clock=time.monotonic, circuit_breaker=None,
):
    """Retry explicitly retryable errors with bounded exponential backoff and jitter."""
    if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 3:
        raise ValueError("max_attempts must be between 1 and 3.")
    if not isinstance(max_elapsed_seconds, (int, float)) or max_elapsed_seconds <= 0:
        raise ValueError("max_elapsed_seconds must be positive.")
    if not isinstance(max_delay_seconds, (int, float)) or max_delay_seconds < 0:
        raise ValueError("max_delay_seconds must be non-negative.")
    retryable = is_retryable or (lambda exc: bool(getattr(exc, "retryable", False)))
    started = clock()
    for attempt in range(max_attempts):
        if circuit_breaker is not None:
            circuit_breaker.before_call()
        if clock() - started >= max_elapsed_seconds:
            raise TimeoutError("Operation exceeded its total execution budget.")
        try:
            result = operation()
            if clock() - started > max_elapsed_seconds:
                raise TimeoutError("Operation exceeded its total execution budget.")
            if circuit_breaker is not None:
                circuit_breaker.record_success()
            return result
        except Exception as exc:
            can_retry = retryable(exc)
            if circuit_breaker is not None:
                if can_retry:
                    circuit_breaker.record_failure()
                else:
                    circuit_breaker.half_open_probe = False
            elapsed = clock() - started
            if elapsed >= max_elapsed_seconds:
                raise TimeoutError("Operation exceeded its total execution budget.") from exc
            if attempt + 1 >= max_attempts or not can_retry:
                raise
            server_delay = retry_after(exc) if callable(retry_after) else getattr(exc, "retry_after", None)
            if server_delay is not None:
                try:
                    delay = max(0.0, float(server_delay))
                except (TypeError, ValueError):
                    delay = 0.0
                if delay > max_delay_seconds:
                    raise
            else:
                base = min(max_delay_seconds, 2.0 ** attempt)
                delay = max(0.0, min(max_delay_seconds, float(jitter(0.0, base))))
            remaining = max_elapsed_seconds - elapsed
            if remaining <= 0:
                raise
            sleep(min(delay, remaining))


class CheckpointStore:
    """Persist stage outputs with run identity, per-stage hashes, and whole-file integrity."""

    def __init__(self, path, request_fingerprint, source_fingerprint, *,
                 run_id=None, team_fingerprint="unspecified", clock=None):
        self.path = Path(path)
        self.request_fingerprint = str(request_fingerprint)
        self.source_fingerprint = str(source_fingerprint)
        self.run_id = str(run_id or os.getenv("GITHUB_RUN_ID") or "local")
        self.team_fingerprint = str(team_fingerprint)
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        self.outputs = {}
        self.metadata = {}
        self.stage_records = {}
        self.created_at = None
        self.updated_at = None
        self.resumed_from_run_id = None
        self._load()

    @staticmethod
    def _canonical(payload):
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

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
                or data.get("source_fingerprint") != self.source_fingerprint
                or data.get("team_fingerprint") != self.team_fingerprint):
            return
        supplied_checksum = data.get("checksum")
        body = {key: value for key, value in data.items() if key != "checksum"}
        expected_checksum = hashlib.sha256(self._canonical(body).encode("utf-8")).hexdigest()
        if not isinstance(supplied_checksum, str) or supplied_checksum != expected_checksum:
            return
        outputs = data.get("outputs")
        metadata = data.get("metadata", {})
        records = data.get("stage_records")
        if (not isinstance(outputs, dict) or len(outputs) > MAX_STAGES
                or not isinstance(metadata, dict) or not isinstance(records, dict)
                or set(outputs) != set(records)):
            return
        clean = {}
        total = 0
        for name, output in outputs.items():
            record = records.get(name)
            if (not isinstance(name, str) or not 1 <= len(name) <= 100
                    or not isinstance(output, str) or len(output) > MAX_STAGE_OUTPUT_CHARS
                    or not isinstance(record, dict)
                    or record.get("sha256") != hashlib.sha256(output.encode("utf-8")).hexdigest()
                    or not isinstance(record.get("completed_at"), str)):
                return
            total += len(name) + len(output)
            if total > MAX_CHECKPOINT_BYTES:
                return
            clean[name] = output
        self.outputs = clean
        self.metadata = metadata
        self.stage_records = records
        self.created_at = data.get("created_at")
        self.updated_at = data.get("updated_at")
        self.resumed_from_run_id = data.get("run_id")

    def _save(self):
        now = self.clock()
        body = {
            "version": CHECKPOINT_VERSION,
            "run_id": self.run_id,
            "request_fingerprint": self.request_fingerprint,
            "source_fingerprint": self.source_fingerprint,
            "team_fingerprint": self.team_fingerprint,
            "created_at": self.created_at or now,
            "updated_at": now,
            "outputs": self.outputs,
            "stage_records": self.stage_records,
            "metadata": self.metadata,
        }
        payload = dict(body)
        payload["checksum"] = hashlib.sha256(self._canonical(body).encode("utf-8")).hexdigest()
        encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if len(encoded.encode("utf-8")) > MAX_CHECKPOINT_BYTES:
            raise ValueError("Checkpoint exceeds the configured size limit.")
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
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
            self.created_at = body["created_at"]
            self.updated_at = body["updated_at"]
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
        self.stage_records[name] = {
            "sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
            "completed_at": self.clock(),
        }
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
        self.stage_records = {}
        self.path.unlink(missing_ok=True)
