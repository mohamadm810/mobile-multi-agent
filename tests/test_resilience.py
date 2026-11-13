import tempfile
import unittest
from pathlib import Path

from agent.resilience import CheckpointStore, call_with_retries


class RetryTests(unittest.TestCase):
    def test_retries_only_explicitly_retryable_failures(self):
        class TemporaryFailure(RuntimeError):
            retryable = True

        attempts = []
        sleeps = []

        def operation():
            attempts.append(1)
            if len(attempts) < 3:
                raise TemporaryFailure("temporary")
            return "recovered"

        result = call_with_retries(operation, max_attempts=3, sleep=sleeps.append)
        self.assertEqual(result, "recovered")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_does_not_retry_non_retryable_failure(self):
        attempts = []

        def operation():
            attempts.append(1)
            raise ValueError("permanent")

        with self.assertRaisesRegex(ValueError, "permanent"):
            call_with_retries(operation, sleep=lambda _: None)
        self.assertEqual(len(attempts), 1)

    def test_attempt_limit_is_bounded(self):
        with self.assertRaisesRegex(ValueError, "between 1 and 3"):
            call_with_retries(lambda: None, max_attempts=10)


class CheckpointTests(unittest.TestCase):
    def test_checkpoint_round_trip_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "CHECKPOINT.json"
            store = CheckpointStore(path, "request-a", "source-a")
            store.record_stage("Requirements Analyst", "verified findings")
            store.set_metadata("retry_count", 1)

            resumed = CheckpointStore(path, "request-a", "source-a")
            self.assertEqual(resumed.get_stage("Requirements Analyst"), "verified findings")
            self.assertEqual(resumed.get_metadata("retry_count"), 1)

    def test_checkpoint_is_rejected_for_changed_request_or_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "CHECKPOINT.json"
            CheckpointStore(path, "request-a", "source-a").record_stage("stage", "output")
            self.assertIsNone(CheckpointStore(path, "request-b", "source-a").get_stage("stage"))
            self.assertIsNone(CheckpointStore(path, "request-a", "source-b").get_stage("stage"))

    def test_corrupt_checkpoint_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "CHECKPOINT.json"
            path.write_text("{broken", encoding="utf-8")
            store = CheckpointStore(path, "request-a", "source-a")
            self.assertEqual(store.outputs, {})

    def test_checkpoint_rejects_oversized_stage_output(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory) / "CHECKPOINT.json", "r", "s")
            with self.assertRaisesRegex(ValueError, "oversized"):
                store.record_stage("stage", "x" * 25_001)

    def test_clear_removes_checkpoint_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "CHECKPOINT.json"
            store = CheckpointStore(path, "r", "s")
            store.record_stage("stage", "output")
            store.clear()
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
