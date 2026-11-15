import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.test_feedback import (
    collect_unittest_result,
    format_test_feedback,
    load_test_feedback,
    write_test_status,
)


def _status(conclusion="success", **overrides):
    payload = {
        "schema": 1,
        "runner": "unittest-subprocess",
        "conclusion": conclusion,
        "tests_run": 2,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "failed_tests": [],
        "duration_seconds": 0.1,
        "head_sha": "local",
        "generated_utc": "2026-11-15T00:00:00+00:00",
        "reason": None,
    }
    payload.update(overrides)
    return payload


class TestFeedbackSchemaTests(unittest.TestCase):
    def test_missing_stale_and_malformed_results_are_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "TEST_STATUS.json"
            self.assertEqual(load_test_feedback(path, expected_sha="local")["reason"], "missing")
            path.write_text("{", encoding="utf-8")
            self.assertEqual(load_test_feedback(path, expected_sha="local")["reason"], "malformed")
            path.write_text(json.dumps(_status(head_sha="other-commit")), encoding="utf-8")
            stale = load_test_feedback(path, expected_sha="abc123")
            self.assertEqual(stale["conclusion"], "unavailable")
            self.assertEqual(stale["reason"], "stale_sha")
            path.write_text("x" * 20_000, encoding="utf-8")
            self.assertEqual(load_test_feedback(path, expected_sha="local")["reason"], "oversized")

    def test_schema_rejects_secret_like_failed_test_names(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "TEST_STATUS.json"
            path.write_text(json.dumps(_status(
                conclusion="failure",
                failures=1,
                failed_tests=["token=super-secret"],
            )), encoding="utf-8")
            loaded = load_test_feedback(path, expected_sha="local")
            self.assertEqual(loaded["conclusion"], "unavailable")
            self.assertNotIn("super-secret", json.dumps(loaded))

    def test_success_and_failure_prompts_repeat_only_the_recorded_result(self):
        success = load_test_feedback_from(_status())
        text = format_test_feedback(success)
        self.assertIn("conclusion=success", text)
        self.assertIn("tests_run=2", text)
        self.assertIn("not the isolated", text)
        failure = load_test_feedback_from(_status(
            conclusion="failure", failures=1, failed_tests=["tests.test_sample.SampleTests.test_fails"],
        ))
        failure_text = format_test_feedback(failure)
        self.assertIn("conclusion=failure", failure_text)
        self.assertIn("tests.test_sample.SampleTests.test_fails", failure_text)
        unavailable = format_test_feedback({"conclusion": "unavailable", "reason": "missing"})
        self.assertIn("unavailable (missing)", unavailable)
        self.assertNotIn("conclusion=success", unavailable)

    def test_runner_timeout_is_recorded_without_output(self):
        def explode(_root):
            raise subprocess.TimeoutExpired(cmd=["unittest"], timeout=1)

        with tempfile.TemporaryDirectory() as directory:
            payload = write_test_status(directory, directory, runner=explode)
            self.assertEqual(payload["conclusion"], "error")
            self.assertEqual(payload["reason"], "timeout")
            self.assertIsNone(payload["tests_run"])
            saved = Path(directory, "TEST_STATUS.json").read_text(encoding="utf-8")
            self.assertNotIn("Traceback", saved)

    def test_real_child_process_counts_a_tiny_suite_and_hides_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_sample.py").write_text(
                "import unittest\n"
                "class SampleTests(unittest.TestCase):\n"
                "    def test_ok(self):\n"
                "        self.assertTrue(True)\n"
                "    def test_fails(self):\n"
                "        raise AssertionError('GEMINI_API_KEY=should-not-persist')\n",
                encoding="utf-8",
            )
            result = collect_unittest_result(root, timeout_seconds=30)
            self.assertEqual(result["conclusion"], "failure")
            self.assertEqual(result["tests_run"], 2)
            self.assertEqual(result["failures"], 1)
            self.assertEqual(result["failed_tests"], ["test_sample.SampleTests.test_fails"])
            blob = json.dumps(result)
            self.assertNotIn("should-not-persist", blob)
            self.assertNotIn("GEMINI_API_KEY", blob)


def load_test_feedback_from(payload):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "TEST_STATUS.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return load_test_feedback(path, expected_sha=payload["head_sha"])


class RunCycleFeedbackTests(unittest.TestCase):
    def test_run_cycle_injects_matching_result_and_ignores_other_commits(self):
        from agent.run import run_cycle

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompts = []
            status_path = root / "TEST_STATUS.json"
            status_path.write_text(json.dumps(_status(head_sha="commit-a")), encoding="utf-8")
            with patch("agent.run.WORKSPACE", root), patch(
                "agent.run.STATE_FILE", root / "STATE.json"
            ), patch("agent.run.REPORT_FILE", root / "REPORT.md"), patch(
                "agent.run.TEAM_FILE", root / "TEAM.json"
            ), patch("agent.run.read_state", return_value={"status": "not_started"}), patch(
                "agent.run.read_repository_context", return_value="source evidence"
            ), patch.dict(os.environ, {"GITHUB_SHA": "commit-b"}, clear=False):
                run_cycle("feedback request", generator=lambda role, prompt: prompts.append(prompt) or "ok")
            self.assertIn("IN-JOB UNITTEST RESULT: unavailable (stale_sha)", prompts[0])
            self.assertNotIn("conclusion=success", prompts[0])
            state = json.loads((root / "STATE.json").read_text(encoding="utf-8"))
            self.assertEqual(state["unit_test_execution"]["conclusion"], "unavailable")
            self.assertEqual(state["unit_test_execution"]["reason"], "stale_sha")

            prompts.clear()
            status_path.write_text(json.dumps(_status(
                conclusion="failure",
                failures=1,
                failed_tests=["tests.test_agent.Example.test_gap"],
                head_sha="commit-a",
            )), encoding="utf-8")
            with patch("agent.run.WORKSPACE", root), patch(
                "agent.run.STATE_FILE", root / "STATE.json"
            ), patch("agent.run.REPORT_FILE", root / "REPORT.md"), patch(
                "agent.run.TEAM_FILE", root / "TEAM.json"
            ), patch("agent.run.read_state", return_value={"status": "not_started"}), patch(
                "agent.run.read_repository_context", return_value="source evidence"
            ), patch.dict(os.environ, {"GITHUB_SHA": "commit-a"}, clear=False):
                run_cycle("feedback request", generator=lambda role, prompt: prompts.append(prompt) or "ok")
            self.assertIn("conclusion=failure", prompts[0])
            self.assertIn("conclusion=failure", prompts[-2])
            self.assertIn("conclusion=failure", prompts[-1])
            report = (root / "REPORT.md").read_text(encoding="utf-8")
            self.assertIn("conclusion=failure", report)
            state = json.loads((root / "STATE.json").read_text(encoding="utf-8"))
            self.assertEqual(state["unit_test_execution"]["tests_run"], 2)
            self.assertEqual(state["unit_test_execution"]["failed_tests"], ["tests.test_agent.Example.test_gap"])
            self.assertIn("do not treat the in-job unittest result as that workflow", prompts[-2])


if __name__ == "__main__":
    unittest.main()
