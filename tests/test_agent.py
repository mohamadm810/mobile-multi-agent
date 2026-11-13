import http.client
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

    def test_malformed_candidate_is_clear_error(self):
        with self.assertRaisesRegex(AgentError, "malformed candidate"):
            _extract_text({"candidates": [None]})

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


    @patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"})
    @patch("agent.gemini.time.sleep")
    @patch("agent.gemini.urllib.request.urlopen")
    def test_exhausts_primary_and_all_fallback_models(self, mocked_urlopen, _sleep):
        import agent.gemini as gemini

        failures = [
            urllib.error.HTTPError(
                "https://example.invalid", 503, "temporarily unavailable", {},
                io.BytesIO(b"temporary outage")
            )
            for _ in range(6)
        ]
        mocked_urlopen.side_effect = failures

        with patch("agent.gemini.MODEL", "primary-model"), patch(
            "agent.gemini.ACTIVE_MODEL", "primary-model"
        ), patch("agent.gemini.FALLBACK_MODELS", ["fallback-model"]):
            gemini.USED_MODELS.clear()
            gemini.ATTEMPTED_MODELS.clear()
            with self.assertRaises(AgentError) as caught:
                generate("role", "prompt")
            self.assertEqual(gemini.ACTIVE_MODEL, "primary-model")

        self.assertIn("HTTP 503 on fallback-model", str(caught.exception))
        self.assertNotIn("test-key", str(caught.exception))
        self.assertEqual(mocked_urlopen.call_count, 6)
        self.assertEqual(gemini.ATTEMPTED_MODELS, ["primary-model", "fallback-model"])
        self.assertEqual(gemini.USED_MODELS, [])



    @patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"})
    @patch("agent.gemini.time.sleep")
    @patch("agent.gemini.urllib.request.urlopen")
    def test_intermediate_fallback_model_can_succeed(self, mocked_urlopen, _sleep):
        import agent.gemini as gemini

        failures = [
            urllib.error.HTTPError(
                "https://example.invalid", 503, "busy", {},
                io.BytesIO(b"temporary outage")
            )
            for _ in range(6)
        ]
        success = MagicMock()
        success.__enter__.return_value.read.return_value = json.dumps({
            "candidates": [{"content": {"parts": [{"text": "third model ok"}]}}]
        }).encode()
        mocked_urlopen.side_effect = [*failures, success]

        with patch("agent.gemini.MODEL", "primary-model"), patch(
            "agent.gemini.ACTIVE_MODEL", "primary-model"
        ), patch(
            "agent.gemini.FALLBACK_MODELS", ["fallback-one", "fallback-two"]
        ):
            gemini.USED_MODELS.clear()
            gemini.ATTEMPTED_MODELS.clear()
            self.assertEqual(generate("role", "prompt"), "third model ok")

        self.assertEqual(mocked_urlopen.call_count, 7)
        self.assertEqual(
            gemini.ATTEMPTED_MODELS,
            ["primary-model", "fallback-one", "fallback-two"],
        )
        self.assertEqual(gemini.USED_MODELS, ["fallback-two"])
        self.assertEqual(gemini.ACTIVE_MODEL, "fallback-two")

    @patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"})
    @patch("agent.gemini.time.sleep")
    @patch("agent.gemini.urllib.request.urlopen")
    def test_incomplete_http_response_retries_and_uses_fallback(self, mocked_urlopen, _sleep):
        interrupted = http.client.IncompleteRead(b"partial", 100)
        failures = [interrupted, interrupted, interrupted]
        success = MagicMock()
        success.__enter__.return_value.read.return_value = json.dumps({
            "candidates": [{"content": {"parts": [{"text": "recovered"}]}}]
        }).encode()
        mocked_urlopen.side_effect = [*failures, success]

        with patch("agent.gemini.MODEL", "primary-model"), patch(
            "agent.gemini.ACTIVE_MODEL", "primary-model"
        ), patch("agent.gemini.FALLBACK_MODELS", ["fallback-model"]):
            self.assertEqual(generate("role", "prompt"), "recovered")

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

    def test_retry_after_is_clamped_to_safe_bounds(self):
        negative = urllib.error.HTTPError(
            "https://example.invalid", 503, "busy", {"Retry-After": "-10"}, None
        )
        excessive = urllib.error.HTTPError(
            "https://example.invalid", 503, "busy", {"Retry-After": "120"}, None
        )
        self.assertEqual(_retry_delay(negative, 1), 1.0)
        self.assertEqual(_retry_delay(excessive, 1), 30.0)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"})
    @patch("agent.gemini.urllib.request.urlopen")
    def test_malformed_json_response_raises_agent_error(self, mocked_urlopen):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"{not-valid-json"
        mocked_urlopen.return_value = response
        with patch("agent.gemini.MODEL", "primary-model"), patch(
            "agent.gemini.ACTIVE_MODEL", "primary-model"
        ), patch("agent.gemini.FALLBACK_MODELS", []):
            with self.assertRaisesRegex(AgentError, "invalid response"):
                generate("role", "prompt")
        self.assertEqual(mocked_urlopen.call_count, 1)


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


class AtomicPersistenceTests(unittest.TestCase):
    def test_atomic_write_replaces_existing_file(self):
        from agent.run import atomic_write_text
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "STATE.json"
            target.write_text('{"status":"old"}', encoding="utf-8")
            atomic_write_text(target, '{"status":"new"}\n')
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")),
                {"status": "new"},
            )
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ["STATE.json"])



class ConcurrentAtomicPersistenceTests(unittest.TestCase):
    def test_concurrent_atomic_writes_never_leave_partial_json(self):
        from concurrent.futures import ThreadPoolExecutor
        from agent.run import atomic_write_text

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "STATE.json"

            def write_version(version):
                payload = json.dumps({"version": version, "values": list(range(100))})
                for _ in range(10):
                    atomic_write_text(target, payload)

            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(write_version, range(8)))

            saved = json.loads(target.read_text(encoding="utf-8"))
            self.assertIn(saved["version"], range(8))
            self.assertEqual(saved["values"], list(range(100)))
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ["STATE.json"])


class OrchestrationTests(unittest.TestCase):
    def test_documentation_agent_receives_deterministic_run_evidence(self):
        from agent.run import run_cycle
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompts = []
            with patch("agent.run.WORKSPACE", root), patch(
                "agent.run.STATE_FILE", root / "STATE.json"
            ), patch("agent.run.REPORT_FILE", root / "REPORT.md"), patch(
                "agent.run.read_state", return_value={"status": "not_started"}
            ), patch("agent.run.read_repository_context", return_value="source evidence"):
                outputs = run_cycle(
                    "test request",
                    generator=lambda role, prompt: prompts.append((role, prompt)) or "ok",
                )
            self.assertEqual(len(outputs), 7)
            docs_prompt = prompts[-1][1]
            self.assertIn("LIVE EXECUTION EVIDENCE", docs_prompt)
            self.assertIn("Source contains", docs_prompt)
            self.assertIn("test_documentation_agent_receives_deterministic_run_evidence", docs_prompt)
            self.assertTrue((root / "REPORT.md").is_file())
            saved_state = json.loads((root / "STATE.json").read_text(encoding="utf-8"))
            self.assertEqual(len(saved_state["completed_agents"]), 7)


class ConfigValidationTests(unittest.TestCase):
    def test_positive_integer_env_parses_valid_value(self):
        from agent.config import _positive_int_env
        with patch.dict(os.environ, {"TEST_POSITIVE_SETTING": "256"}):
            self.assertEqual(_positive_int_env("TEST_POSITIVE_SETTING", 10), 256)

    def test_positive_integer_env_rejects_invalid_values(self):
        from agent.config import _positive_int_env
        for invalid in ("", "abc", "1.5", "0", "-4"):
            with self.subTest(value=invalid), patch.dict(
                os.environ, {"TEST_POSITIVE_SETTING": invalid}
            ):
                with self.assertRaisesRegex(
                    ValueError, "TEST_POSITIVE_SETTING must be a positive integer"
                ):
                    _positive_int_env("TEST_POSITIVE_SETTING", 10)

    def test_positive_integer_env_uses_default_when_unset(self):
        from agent.config import _positive_int_env
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_positive_int_env("UNSET_POSITIVE_SETTING", 17), 17)


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
