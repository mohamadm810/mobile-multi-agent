import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
