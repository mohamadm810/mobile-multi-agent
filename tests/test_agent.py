import json
import os
import unittest
import urllib.error
from unittest.mock import patch, MagicMock

from agent.gemini import AgentError, _extract_text, generate


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
