import io
import json
import os
import tempfile
import unittest
from pathlib import Path
import urllib.error
from unittest.mock import patch, MagicMock

from agent.gemini import AgentError, _extract_text, _retry_delay, generate, reset_run_tracking


class GeminiResponseTests(unittest.TestCase):
    def test_extracts_all_text_parts(self):
        data = {"candidates": [{"content": {"parts": [
            {"text": "first"}, {"inline_data": {}}, {"text": "second"}
        ]}}]}
        self.assertEqual(_extract_text(data), "first\nsecond")

    def test_empty_response_is_clear_error(self):
        with self.assertRaisesRegex(AgentError, "empty"):
            _extract_text({"candidates": [{"content": {"parts": []}}]})

    def test_missing_candidates_is_clear_error(self):
        with self.assertRaisesRegex(AgentError, "no candidates"):
            _extract_text({})

    @patch.dict(os.environ, {}, clear=True)
    def test_missing_key_fails_before_network(self):
        with self.assertRaisesRegex(AgentError, "Missing GEMINI_API_KEY"):
            generate("role", "prompt")

    @patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"})
    @patch("agent.gemini.time.sleep")
    @patch("agent.gemini.urllib.request.urlopen")
    def test_uses_fallback_model_after_primary_503(self, mocked_urlopen, _sleep):
        errors = [urllib.error.HTTPError("https://example.invalid", 503, "busy", {}, None) for _ in range(3)]
        success = MagicMock()
        success.__enter__.return_value.read.return_value = json.dumps({
            "candidates": [{"content": {"parts": [{"text": "fallback ok"}]}}]
        }).encode()
        success_again = MagicMock()
        success_again.__enter__.return_value.read.return_value = json.dumps({
            "candidates": [{"content": {"parts": [{"text": "second ok"}]}}]
        }).encode()
        mocked_urlopen.side_effect = [*errors, success, success_again]
        with patch("agent.gemini.MODEL", "primary-model"), patch(
            "agent.gemini.ACTIVE_MODEL", "primary-model"
        ), patch("agent.gemini.FALLBACK_MODELS", ["fallback-model"]):
            self.assertEqual(generate("role", "prompt"), "fallback ok")
            self.assertEqual(generate("role", "prompt"), "second ok")
        self.assertEqual(mocked_urlopen.call_count, 5)
        self.assertIn("fallback-model", mocked_urlopen.call_args_list[-1].args[0].full_url)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"})
    @patch("agent.gemini.time.sleep")
    @patch("agent.gemini.urllib.request.urlopen")
    def test_transport_errors_retry_then_use_fallback(self, mocked_urlopen, _sleep):
        failures = [urllib.error.URLError("temporary network failure") for _ in range(3)]
        success = MagicMock()
        success.__enter__.return_value.read.return_value = json.dumps({
            "candidates": [{"content": {"parts": [{"text": "network fallback ok"}]}}]
        }).encode()
        mocked_urlopen.side_effect = [*failures, success]
        with patch("agent.gemini.MODEL", "primary-model"), patch(
            "agent.gemini.ACTIVE_MODEL", "primary-model"
        ), patch("agent.gemini.FALLBACK_MODELS", ["fallback-model"]):
            self.assertEqual(generate("role", "prompt"), "network fallback ok")
        self.assertEqual(mocked_urlopen.call_count, 4)
        self.assertIn("fallback-model", mocked_urlopen.call_args_list[-1].args[0].full_url)

    def test_reset_run_tracking_clears_models_and_restores_primary(self):
        import agent.gemini as gemini
        gemini.USED_MODELS.append("fallback-model")
        gemini.ATTEMPTED_MODELS.append("primary-model")
        gemini.ACTIVE_MODEL = "fallback-model"
        reset_run_tracking()
        self.assertEqual(gemini.USED_MODELS, [])
        self.assertEqual(gemini.ATTEMPTED_MODELS, [])
        self.assertEqual(gemini.ACTIVE_MODEL, gemini.MODEL)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"})
    @patch("agent.gemini.urllib.request.urlopen")
    def test_non_retryable_403_fails_without_fallback(self, mocked_urlopen):
        mocked_urlopen.side_effect = urllib.error.HTTPError(
            "https://example.invalid", 403, "forbidden", {}, None
        )
        with patch("agent.gemini.MODEL", "primary-model"), patch(
            "agent.gemini.ACTIVE_MODEL", "primary-model"
        ), patch("agent.gemini.FALLBACK_MODELS", ["fallback-model"]):
            with self.assertRaisesRegex(AgentError, "HTTP 403"):
                generate("role", "prompt")
        self.assertEqual(mocked_urlopen.call_count, 1)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"})
    @patch("agent.gemini.urllib.request.urlopen")
    def test_http_error_detail_redacts_api_key(self, mocked_urlopen):
        mocked_urlopen.side_effect = urllib.error.HTTPError(
            "https://example.invalid", 403, "forbidden", {},
            io.BytesIO(b"server echoed test-key in its response")
        )
        with patch("agent.gemini.MODEL", "primary-model"), patch(
            "agent.gemini.ACTIVE_MODEL", "primary-model"
        ), patch("agent.gemini.FALLBACK_MODELS", []):
            with self.assertRaises(AgentError) as caught:
                generate("role", "prompt")
        self.assertNotIn("test-key", str(caught.exception))
        self.assertIn("[REDACTED]", str(caught.exception))

    @patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"})
    @patch("agent.gemini.urllib.request.urlopen")
    def test_response_over_2mb_is_rejected(self, mocked_urlopen):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"x" * 2_000_001
        mocked_urlopen.return_value = response
        with patch("agent.gemini.MODEL", "primary-model"), patch(
            "agent.gemini.ACTIVE_MODEL", "primary-model"
        ), patch("agent.gemini.FALLBACK_MODELS", []):
            with self.assertRaisesRegex(AgentError, "2 MB safety limit"):
                generate("role", "prompt")
        self.assertEqual(mocked_urlopen.call_count, 1)

    def test_invalid_retry_after_uses_exponential_default(self):
        error = urllib.error.HTTPError(
            "https://example.invalid", 503, "busy",
            {"Retry-After": "not-a-number"}, None
        )
        self.assertEqual(_retry_delay(error, 2), 4.0)


class FailureStateTests(unittest.TestCase):
    def test_failure_status_is_persisted(self):
        from agent.run import write_failure_state
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "STATE.json"
            with patch("agent.run.STATE_FILE", state_path), patch(
                "agent.run.read_state", return_value={"status": "not_started"}
            ):
                write_failure_state(AgentError("Gemini API HTTP 503"))
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "failed")
            self.assertIn("HTTP 503", saved["last_error"])

    @patch.dict(os.environ, {"GEMINI_API_KEY": "private-test-secret"})
    @patch("agent.run.write_failure_state")
    @patch("agent.run.run_cycle", side_effect=ValueError("unexpected private-test-secret"))
    def test_main_persists_unexpected_failure_without_secret(self, _run, write_state):
        from agent.run import main
        with self.assertRaises(SystemExit) as caught:
            main()
        self.assertEqual(caught.exception.code, 1)
        saved_error = write_state.call_args.args[0]
        self.assertIn("ValueError", saved_error)
        self.assertNotIn("private-test-secret", saved_error)


class RepositoryEvidenceTests(unittest.TestCase):
    def test_repository_context_includes_complete_test_file(self):
        from agent.run import read_repository_context
        context = read_repository_context()
        self.assertIn("def test_non_retryable_403_fails_without_fallback", context)
        self.assertIn("if __name__ == \"__main__\":", context)


class RequestValidationTests(unittest.TestCase):
    def test_rejects_empty_request(self):
        from agent.run import run_cycle
        with self.assertRaisesRegex(AgentError, "empty"):
            run_cycle("   ", generator=lambda *_: "ok")

    def test_rejects_oversized_request(self):
        from agent.run import run_cycle
        with self.assertRaises(AgentError):
            run_cycle("x" * 12001, generator=lambda *_: "ok")


if __name__ == "__main__":
    unittest.main()
