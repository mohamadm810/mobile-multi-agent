import tempfile
import unittest
from pathlib import Path

from unittest.mock import patch

from agent.preflight import main, run_preflight


class PreflightTests(unittest.TestCase):
    def make_root(self, directory):
        root = Path(directory)
        for relative in (
            "agent/run.py",
            "agent/gemini.py",
            "agent/resilience.py",
            ".github/workflows/run-agent.yml",
        ):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# fixture\n", encoding="utf-8")
        request = root / "workspace" / "REQUEST.md"
        request.parent.mkdir(parents=True, exist_ok=True)
        request.write_text("Build a small test project plan.\n", encoding="utf-8")
        return root

    def test_ready_with_gemini_and_independent_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            result = run_preflight(root, {
                "GEMINI_API_KEY": "gemini-secret",
                "OPENAI_API_KEY": "openai-secret",
            })
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["model_requests_made"], 0)
        self.assertTrue(all(check["ok"] for check in result["checks"]))
        self.assertNotIn("gemini-secret", str(result))
        self.assertNotIn("openai-secret", str(result))

    def test_ready_with_only_one_configured_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            result = run_preflight(root, {"GEMINI_API_KEY": "gemini-secret"})
        self.assertEqual(result["status"], "ready")

    def test_blocks_before_run_when_no_provider_secret_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            result = run_preflight(root, {})
        self.assertEqual(result["status"], "blocked")
        self.assertIn("provider_credentials", result["blocking_checks"])

    def test_blocks_invalid_openai_endpoint_when_fallback_is_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            result = run_preflight(root, {
                "GEMINI_API_KEY": "gemini-secret",
                "OPENAI_API_KEY": "openai-secret",
                "OPENAI_BASE_URL": "http://insecure.example/v1",
            })
        self.assertEqual(result["status"], "blocked")
        self.assertIn("openai_endpoint", result["blocking_checks"])

    def test_blocks_missing_request_and_required_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            (root / "workspace" / "REQUEST.md").unlink()
            (root / "agent" / "resilience.py").unlink()
            result = run_preflight(root, {"GEMINI_API_KEY": "gemini-secret"})
        self.assertEqual(result["status"], "blocked")
        self.assertIn("project_request", result["blocking_checks"])
        self.assertIn("required_file:agent/resilience.py", result["blocking_checks"])

    def test_main_persists_blocked_preflight_result_for_operator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            with patch("agent.preflight.ROOT", root), patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(SystemExit) as caught:
                    main()
            self.assertEqual(caught.exception.code, 1)
            saved = __import__("json").loads(
                (root / "workspace" / "PREFLIGHT.json").read_text(encoding="utf-8")
            )
            self.assertEqual(saved["status"], "blocked")
            self.assertEqual(saved["model_requests_made"], 0)
            self.assertTrue(saved["checked_at_utc"])


if __name__ == "__main__":
    unittest.main()
