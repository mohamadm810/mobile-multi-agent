import json
import tempfile
import unittest
from pathlib import Path

from agent.resilience import (
    CheckpointStore, CircuitBreaker, CircuitOpenError, call_with_retries,
)


class RetryTests(unittest.TestCase):
    def test_retries_only_explicitly_retryable_failures(self):
        class TemporaryFailure(RuntimeError):
            retryable = True

        attempts, sleeps = [], []

        def operation():
            attempts.append(1)
            if len(attempts) < 3:
                raise TemporaryFailure("temporary")
            return "recovered"

        result = call_with_retries(
            operation, max_attempts=3, sleep=sleeps.append,
            jitter=lambda low, high: high,
        )
        self.assertEqual(result, "recovered")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_retry_after_is_honored_and_capped(self):
        class TemporaryFailure(RuntimeError):
            retryable = True
            retry_after = 4.5

        sleeps = []
        attempts = []

        def operation():
            attempts.append(1)
            if len(attempts) == 1:
                raise TemporaryFailure("busy")
            return "ok"

        self.assertEqual(call_with_retries(operation, sleep=sleeps.append), "ok")
        self.assertEqual(sleeps, [4.5])

    def test_retry_after_beyond_budget_is_not_violated_by_early_retry(self):
        class TemporaryFailure(RuntimeError):
            retryable = True
            retry_after = 999

        sleeps = []
        with self.assertRaises(TemporaryFailure):
            call_with_retries(
                lambda: (_ for _ in ()).throw(TemporaryFailure("busy")),
                sleep=sleeps.append, max_delay_seconds=3,
            )
        self.assertEqual(sleeps, [])

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

    def test_total_execution_budget_is_enforced_between_attempts(self):
        class TemporaryFailure(RuntimeError):
            retryable = True

        now = [0.0]
        attempts = []

        def operation():
            attempts.append(1)
            now[0] += 2.0
            raise TemporaryFailure("still down")

        with self.assertRaises(TimeoutError):
            call_with_retries(
                operation, max_attempts=3, sleep=lambda delay: now.__setitem__(0, now[0] + delay),
                jitter=lambda low, high: high, max_elapsed_seconds=1.0,
                clock=lambda: now[0],
            )
        self.assertEqual(len(attempts), 1)

    def test_circuit_breaker_opens_and_recovers_after_cooldown(self):
        now = [0.0]
        breaker = CircuitBreaker(failure_threshold=2, reset_timeout=5, clock=lambda: now[0])
        breaker.record_failure()
        breaker.record_failure()
        with self.assertRaises(CircuitOpenError):
            breaker.before_call()
        now[0] = 5.0
        breaker.before_call()
        with self.assertRaises(CircuitOpenError):
            breaker.before_call()
        breaker.record_success()
        breaker.before_call()

    def test_retryable_failures_trip_circuit_breaker(self):
        class TemporaryFailure(RuntimeError):
            retryable = True

        breaker = CircuitBreaker(failure_threshold=1, reset_timeout=30)
        with self.assertRaises(TemporaryFailure):
            call_with_retries(
                lambda: (_ for _ in ()).throw(TemporaryFailure("down")),
                max_attempts=1, circuit_breaker=breaker,
            )
        with self.assertRaises(CircuitOpenError):
            call_with_retries(lambda: "should not run", circuit_breaker=breaker)


class CheckpointTests(unittest.TestCase):
    def test_test_inventory_includes_all_test_files(self):
        from agent.run import get_unit_test_inventory

        inventory = get_unit_test_inventory()
        self.assertTrue(any(name.startswith("tests/test_agent.py:") for name in inventory))
        self.assertTrue(any(name.startswith("tests/test_resilience.py:") for name in inventory))
        self.assertTrue(any(name.startswith("tests/test_patcher.py:") for name in inventory))

    def test_checkpoint_round_trip_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "CHECKPOINT.json"
            store = CheckpointStore(path, "request-a", "source-a", run_id="run-1", team_fingerprint="team-a")
            store.record_stage("Requirements Analyst", "verified findings")
            store.set_metadata("retry_count", 1)

            resumed = CheckpointStore(path, "request-a", "source-a", run_id="run-2", team_fingerprint="team-a")
            self.assertEqual(resumed.get_stage("Requirements Analyst"), "verified findings")
            self.assertEqual(resumed.get_metadata("retry_count"), 1)
            self.assertEqual(resumed.resumed_from_run_id, "run-1")
            self.assertIsNotNone(resumed.stage_records["Requirements Analyst"]["completed_at"])

    def test_checkpoint_is_rejected_for_changed_request_source_or_team(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "CHECKPOINT.json"
            CheckpointStore(path, "request-a", "source-a", team_fingerprint="team-a").record_stage("stage", "output")
            self.assertIsNone(CheckpointStore(path, "request-b", "source-a", team_fingerprint="team-a").get_stage("stage"))
            self.assertIsNone(CheckpointStore(path, "request-a", "source-b", team_fingerprint="team-a").get_stage("stage"))
            self.assertIsNone(CheckpointStore(path, "request-a", "source-a", team_fingerprint="team-b").get_stage("stage"))

    def test_checkpoint_detects_modified_stage_output_and_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "CHECKPOINT.json"
            CheckpointStore(path, "r", "s").record_stage("stage", "trusted")
            data = json.loads(path.read_text(encoding="utf-8"))
            data["outputs"]["stage"] = "tampered"
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertIsNone(CheckpointStore(path, "r", "s").get_stage("stage"))

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

    def test_interrupted_run_resumes_completed_agent_stages(self):
        from unittest.mock import patch
        from agent.run import run_cycle

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            common_patches = [
                patch("agent.run.WORKSPACE", root),
                patch("agent.run.STATE_FILE", root / "STATE.json"),
                patch("agent.run.REPORT_FILE", root / "REPORT.md"),
                patch("agent.run.TEAM_FILE", root / "TEAM.json"),
                patch("agent.run.read_state", return_value={"status": "not_started"}),
                patch("agent.run.read_repository_context", return_value="source evidence"),
            ]
            first_calls = []

            def failing_generator(role, prompt):
                first_calls.append(role)
                if len(first_calls) == 2:
                    raise RuntimeError("simulated interruption")
                return "first stage completed"

            with common_patches[0], common_patches[1], common_patches[2], common_patches[3], common_patches[4], common_patches[5]:
                with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                    run_cycle("resume this request", generator=failing_generator)

                checkpoint = json.loads((root / "CHECKPOINT.json").read_text(encoding="utf-8"))
                self.assertIn("Requirements Analyst", checkpoint["outputs"])

                second_calls = []

                def successful_generator(role, prompt):
                    second_calls.append(role)
                    if role == __import__("agent.roles", fromlist=["HR"]).HR:
                        return json.dumps({"summary": "complete", "manager_review": "none", "actions": []})
                    return "completed"

                run_cycle("resume this request", generator=successful_generator)
                self.assertNotIn(first_calls[0], second_calls)
                self.assertFalse((root / "CHECKPOINT.json").exists())
                saved_state = json.loads((root / "STATE.json").read_text(encoding="utf-8"))
                self.assertIn("Requirements Analyst", saved_state["resumed_agents"])

    def test_checkpointed_registry_is_revalidated_before_replay(self):
        from agent.run import normalize_team_registry

        registry = normalize_team_registry({
            "custom_agents": [
                {"name": "Central Supervisor", "mission": "Protected role",
                 "prompt": "A valid length prompt " * 4},
                {"name": "Allowed Specialist", "mission": "Review source evidence",
                 "prompt": "A valid length prompt " * 4},
            ],
            "role_overrides": {
                "Central Supervisor": "Attempted manager override " * 4,
                "Independent Reviewer": "A valid replacement prompt " * 4,
            },
            "disabled_agents": ["Central Supervisor", "Independent Tester"],
        })
        self.assertEqual([item["name"] for item in registry["custom_agents"]], ["Allowed Specialist"])
        self.assertNotIn("Central Supervisor", registry["role_overrides"])
        self.assertEqual(registry["disabled_agents"], ["Independent Tester"])

    def test_clear_removes_checkpoint_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "CHECKPOINT.json"
            store = CheckpointStore(path, "r", "s")
            store.record_stage("stage", "output")
            store.clear()
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
