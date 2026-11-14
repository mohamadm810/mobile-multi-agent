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

    def test_malformed_content_parts_is_clear_error(self):
        with self.assertRaisesRegex(AgentError, "malformed content parts"):
            _extract_text({"candidates": [{"content": {"parts": None}}]})

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
            self.assertEqual(gemini.ACTIVE_MODEL, "fallback-two")

        self.assertEqual(mocked_urlopen.call_count, 7)
        self.assertEqual(
            gemini.ATTEMPTED_MODELS,
            ["primary-model", "fallback-one", "fallback-two"],
        )
        self.assertEqual(gemini.USED_MODELS, ["fallback-two"])

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


    @patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"})
    @patch("agent.gemini.urllib.request.urlopen")
    def test_malformed_primary_response_uses_fallback_model(self, mocked_urlopen):
        malformed = MagicMock()
        malformed.__enter__.return_value.read.return_value = b"{not-valid-json"
        success = MagicMock()
        success.__enter__.return_value.read.return_value = json.dumps({
            "candidates": [{"content": {"parts": [{"text": "recovered by fallback"}]}}]
        }).encode()
        mocked_urlopen.side_effect = [malformed, success]
        with patch("agent.gemini.MODEL", "primary-model"), patch(
            "agent.gemini.ACTIVE_MODEL", "primary-model"
        ), patch("agent.gemini.FALLBACK_MODELS", ["fallback-model"]):
            self.assertEqual(generate("role", "prompt"), "recovered by fallback")
        self.assertEqual(mocked_urlopen.call_count, 2)
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

    def test_invalid_retry_after_uses_jittered_exponential_default(self):
        error = urllib.error.HTTPError(
            "https://example.invalid", 503, "busy",
            {"Retry-After": "not-a-number"}, None
        )
        with patch("agent.gemini.random.uniform", return_value=4.0) as jitter:
            self.assertEqual(_retry_delay(error, 2), 4.0)
        jitter.assert_called_once_with(0.0, 2.0)

    def test_negative_retry_after_uses_jitter_and_excessive_delay_is_not_retried_early(self):
        negative = urllib.error.HTTPError(
            "https://example.invalid", 503, "busy", {"Retry-After": "-10"}, None
        )
        excessive = urllib.error.HTTPError(
            "https://example.invalid", 503, "busy", {"Retry-After": "120"}, None
        )
        with patch("agent.gemini.random.uniform", return_value=0.5):
            self.assertEqual(_retry_delay(negative, 1), 0.5)
        self.assertIsNone(_retry_delay(excessive, 1))


    def test_http_date_retry_after_is_parsed(self):
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        future = datetime.now(timezone.utc) + timedelta(seconds=8)
        error = urllib.error.HTTPError(
            "https://example.invalid", 503, "busy",
            {"Retry-After": format_datetime(future, usegmt=True)}, None
        )
        delay = _retry_delay(error, 1)
        self.assertIsNotNone(delay)
        self.assertGreaterEqual(delay, 0.0)
        self.assertLessEqual(delay, 30.0)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"})
    @patch("agent.gemini.time.sleep")
    @patch("agent.gemini.urllib.request.urlopen")
    def test_long_retry_after_stops_primary_attempt_without_early_retry(self, mocked_urlopen, mocked_sleep):
        mocked_urlopen.side_effect = urllib.error.HTTPError(
            "https://example.invalid", 429, "busy", {"Retry-After": "120"}, None
        )
        with patch("agent.gemini.MODEL", "primary-model"), patch(
            "agent.gemini.ACTIVE_MODEL", "primary-model"
        ), patch("agent.gemini.FALLBACK_MODELS", []):
            with self.assertRaisesRegex(AgentError, "HTTP 429"):
                generate("role", "prompt")
        self.assertEqual(mocked_urlopen.call_count, 1)
        mocked_sleep.assert_not_called()

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

    @patch.dict(os.environ, {
        "GEMINI_API_KEY": "gemini-private-test",
        "OPENAI_API_KEY": "openai-private-test",
    })
    def test_failure_state_redacts_provider_keys_before_persisting(self):
        from agent.run import write_failure_state
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "STATE.json"
            with patch("agent.run.WORKSPACE", root / "workspace"), patch(
                "agent.run.STATE_FILE", state_path
            ), patch("agent.run.REQUEST_FILE", root / "missing-request.md"), patch(
                "agent.run.TEAM_FILE", root / "TEAM.json"
            ):
                write_failure_state(RuntimeError(
                    "unexpected gemini-private-test and openai-private-test"
                ))
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertNotIn("gemini-private-test", saved["last_error"])
            self.assertNotIn("openai-private-test", saved["last_error"])
            self.assertEqual(saved["last_error"].count("[REDACTED]"), 2)

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
    def test_hr_retries_transient_failure_with_bounded_prompt(self):
        from agent.run import run_cycle
        from agent.roles import HR

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hr_calls = []
            large_source_evidence = "PRIVATE_SOURCE_EVIDENCE " + ("x" * 5000)

            def generator(role, prompt):
                if role == HR:
                    hr_calls.append(prompt)
                    if len(hr_calls) == 1:
                        raise AgentError("temporary provider outage", retryable=True)
                    return json.dumps({
                        "summary": "No staffing changes are justified by this run.",
                        "manager_review": "none",
                        "actions": [],
                    })
                return "role output"

            with patch("agent.run.WORKSPACE", root), patch(
                "agent.run.STATE_FILE", root / "STATE.json"
            ), patch("agent.run.REPORT_FILE", root / "REPORT.md"), patch(
                "agent.run.read_state", return_value={"status": "not_started"}
            ), patch(
                "agent.run.read_repository_context", return_value=large_source_evidence
            ), patch("agent.run.TEAM_FILE", root / "TEAM.json"):
                outputs = run_cycle("test request", generator=generator)

            self.assertEqual(len(outputs), 8)
            self.assertEqual(len(hr_calls), 2)
            self.assertLess(len(hr_calls[-1]), 12000)
            self.assertNotIn("PRIVATE_SOURCE_EVIDENCE", hr_calls[-1])
            saved_state = json.loads((root / "STATE.json").read_text(encoding="utf-8"))
            self.assertEqual(saved_state["hr_status"], "valid")
            self.assertNotIn("Agent Recruitment & Governance", saved_state["degraded_agents"])


    def test_documentation_agent_receives_deterministic_run_evidence(self):
        from agent.run import run_cycle, get_unit_test_inventory
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompts = []
            with patch("agent.run.WORKSPACE", root), patch(
                "agent.run.STATE_FILE", root / "STATE.json"
            ), patch("agent.run.REPORT_FILE", root / "REPORT.md"), patch(
                "agent.run.read_state", return_value={"status": "not_started"}
            ), patch("agent.run.read_repository_context", return_value="source evidence"), patch(
                "agent.run.TEAM_FILE", root / "TEAM.json"
            ):
                outputs = run_cycle(
                    "test request",
                    generator=lambda role, prompt: prompts.append((role, prompt)) or "ok",
                )
            self.assertEqual(len(outputs), 8)
            docs_prompt = prompts[-2][1]
            self.assertIn("LIVE EXECUTION EVIDENCE", docs_prompt)
            self.assertIn("DETERMINISTIC TEST INVENTORY", docs_prompt)
            self.assertRegex(
                docs_prompt,
                r"DETERMINISTIC TEST INVENTORY: Exactly \\d+ unittest methods are present",
            )
            self.assertNotIn("Source contains", docs_prompt)
            self.assertNotIn("test_documentation_agent_receives_deterministic_run_evidence", docs_prompt)
            self.assertTrue((root / "REPORT.md").is_file())
            saved_state = json.loads((root / "STATE.json").read_text(encoding="utf-8"))
            self.assertEqual(len(saved_state["completed_agents"]), 8)
            self.assertEqual(saved_state["hr_status"], "rejected")


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
    def test_read_request_rejects_html_comment_only_template(self):
        from agent.run import read_request
        with tempfile.TemporaryDirectory() as directory:
            request_file = Path(directory) / "REQUEST.md"
            request_file.write_text("<!-- template only -->", encoding="utf-8")
            with patch("agent.run.REQUEST_FILE", request_file):
                with self.assertRaisesRegex(AgentError, "empty/template-only"):
                    read_request()

    def test_read_request_keeps_real_text_after_html_comment(self):
        from agent.run import read_request
        with tempfile.TemporaryDirectory() as directory:
            request_file = Path(directory) / "REQUEST.md"
            request_file.write_text("<!-- template note -->\nPlease audit the retry flow.", encoding="utf-8")
            with patch("agent.run.REQUEST_FILE", request_file):
                self.assertEqual(read_request(), "Please audit the retry flow.")

    def test_read_request_rejects_oversized_raw_content(self):
        from agent.run import read_request
        with tempfile.TemporaryDirectory() as directory:
            request_file = Path(directory) / "REQUEST.md"
            request_file.write_text("<!-- " + ("x" * 12001) + " -->", encoding="utf-8")
            with patch("agent.run.REQUEST_FILE", request_file):
                with self.assertRaises(AgentError):
                    read_request()

    def test_rejects_empty_request(self):
        from agent.run import run_cycle
        with self.assertRaisesRegex(AgentError, "empty"):
            run_cycle("   ", generator=lambda *_: "ok")

    def test_rejects_oversized_request(self):
        from agent.run import run_cycle
        with self.assertRaises(AgentError):
            run_cycle("x" * 12001, generator=lambda *_: "ok")


class AgentGovernanceTests(unittest.TestCase):
    def test_hr_can_hire_custom_agent_for_next_cycle(self):
        from agent.run import apply_hr_decisions
        registry = {"version": 1, "custom_agents": [], "archived_agents": []}
        prompt = "You are a specialist. " + ("Follow evidence and report findings. " * 3)
        payload = json.dumps({
            "summary": "A recurring need requires a specialist.",
            "manager_review": "none",
            "actions": [{"action": "hire", "name": "ASTM Specialist",
                         "mission": "Review ASTM method requirements and evidence.",
                         "prompt": prompt}],
        })
        decision, applied = apply_hr_decisions(payload, registry)
        self.assertEqual(len(registry["custom_agents"]), 1)
        self.assertEqual(registry["custom_agents"][0]["name"], "ASTM Specialist")
        self.assertIn("next cycle", applied[0])

    def test_hr_can_retire_custom_agent_and_archive_record(self):
        from agent.run import apply_hr_decisions
        registry = {"version": 1, "custom_agents": [{
            "name": "Old Specialist", "mission": "Old task",
            "prompt": "A sufficiently long prompt " + ("with clear instructions " * 3),
        }], "archived_agents": []}
        payload = json.dumps({
            "summary": "Role overlap is evidenced.",
            "manager_review": "none",
            "actions": [{"action": "retire", "name": "Old Specialist",
                         "reason": "Its duties duplicate the independent reviewer."}],
        })
        _, applied = apply_hr_decisions(payload, registry)
        self.assertEqual(registry["custom_agents"], [])
        self.assertEqual(registry["archived_agents"][0]["name"], "Old Specialist")
        self.assertIn("archived", applied[0])

    def test_hr_cannot_change_central_supervisor(self):
        from agent.run import apply_hr_decisions
        registry = {"version": 1, "custom_agents": [], "archived_agents": []}
        payload = json.dumps({
            "actions": [{"action": "retire", "name": "Central Supervisor",
                         "reason": "The manager seems inefficient."}],
        })
        with self.assertRaisesRegex(ValueError, "Central Supervisor"):
            apply_hr_decisions(payload, registry)

    def test_hr_rejects_too_many_staffing_actions(self):
        from agent.run import apply_hr_decisions
        registry = {"version": 1, "custom_agents": [], "archived_agents": []}
        payload = json.dumps({"actions": [
            {"action": "hire", "name": "Specialist One", "mission": "One specialist role",
             "prompt": "A sufficiently long prompt " + ("with instructions " * 4)},
            {"action": "hire", "name": "Specialist Two", "mission": "Another specialist role",
             "prompt": "A sufficiently long prompt " + ("with instructions " * 4)},
            {"action": "hire", "name": "Specialist Three", "mission": "Third specialist role",
             "prompt": "A sufficiently long prompt " + ("with instructions " * 4)},
        ]})
        with self.assertRaisesRegex(ValueError, "action limit"):
            apply_hr_decisions(payload, registry)


    def test_hr_can_revise_non_manager_core_role(self):
        from agent.run import apply_hr_decisions
        registry = {"version": 1, "custom_agents": [], "archived_agents": [], "role_overrides": {}}
        revised = "You are a revised specialist reviewer. " + ("Use evidence and measurable criteria. " * 3)
        payload = json.dumps({"actions": [{
            "action": "revise", "name": "Independent Reviewer", "prompt": revised,
        }]})
        _, applied = apply_hr_decisions(payload, registry)
        self.assertEqual(registry["role_overrides"]["Independent Reviewer"], revised)
        self.assertIn("next cycle", applied[0])

    def test_hr_cannot_revise_manager_role(self):
        from agent.run import apply_hr_decisions
        registry = {"version": 1, "custom_agents": [], "archived_agents": [], "role_overrides": {}}
        payload = json.dumps({"actions": [{
            "action": "revise", "name": "Central Supervisor",
            "prompt": "A sufficiently long replacement instruction " * 3,
        }]})
        with self.assertRaisesRegex(ValueError, "Central Supervisor"):
            apply_hr_decisions(payload, registry)


    def test_hr_cannot_hire_duplicate_core_role(self):
        from agent.run import apply_hr_decisions
        registry = {"version": 1, "custom_agents": [], "archived_agents": [], "role_overrides": {}}
        payload = json.dumps({"actions": [{
            "action": "hire", "name": " Independent Reviewer ",
            "mission": "A duplicate reviewer role", 
            "prompt": "A sufficiently long specialist prompt " * 3,
        }]})
        with self.assertRaisesRegex(ValueError, "duplicate of a protected core role"):
            apply_hr_decisions(payload, registry)


    def test_hr_can_retire_non_manager_core_role(self):
        from agent.run import apply_hr_decisions
        registry = {"version": 1, "custom_agents": [], "archived_agents": [],
                    "role_overrides": {}, "disabled_agents": []}
        payload = json.dumps({"actions": [{
            "action": "retire", "name": "Independent Tester",
            "reason": "Repeated evidence shows its checks duplicate the independent reviewer.",
        }]})
        _, applied = apply_hr_decisions(payload, registry)
        self.assertEqual(registry["disabled_agents"], ["Independent Tester"])
        self.assertIn("disabled", applied[0])

    def test_hr_can_restore_retired_core_role(self):
        from agent.run import apply_hr_decisions
        registry = {"version": 1, "custom_agents": [], "archived_agents": [],
                    "role_overrides": {}, "disabled_agents": ["Independent Tester"]}
        payload = json.dumps({"actions": [{"action": "restore", "name": "Independent Tester"}]})
        _, applied = apply_hr_decisions(payload, registry)
        self.assertEqual(registry["disabled_agents"], [])
        self.assertIn("Restored", applied[0])

    def test_hr_can_restore_archived_custom_agent(self):
        from agent.run import apply_hr_decisions
        prompt = "You are a specialist. " + ("Follow evidence and report findings. " * 3)
        registry = {"version": 1, "custom_agents": [], "role_overrides": {}, "disabled_agents": [],
                    "archived_agents": [{"name": "Archived Specialist", "mission": "Specialist review",
                                        "prompt": prompt, "kind": "custom"}]}
        payload = json.dumps({"actions": [{"action": "restore", "name": "Archived Specialist"}]})
        _, applied = apply_hr_decisions(payload, registry)
        self.assertEqual(registry["custom_agents"][0]["name"], "Archived Specialist")
        self.assertEqual(registry["archived_agents"], [])
        self.assertIn("Restored", applied[0])

    def test_hr_rejects_invalid_json_without_changes(self):
        from agent.run import apply_hr_decisions
        registry = {"version": 1, "custom_agents": [], "archived_agents": []}
        with self.assertRaises(json.JSONDecodeError):
            apply_hr_decisions("not json", registry)
        self.assertEqual(registry["custom_agents"], [])




    @patch.dict(os.environ, {"OPENAI_API_KEY": "openai-test-key"}, clear=True)
    @patch("agent.gemini.urllib.request.urlopen")
    def test_openai_compatible_provider_works_without_gemini_secret(self, mocked_urlopen):
        import agent.gemini as gemini
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": "independent provider ok"}}]
        }).encode()
        mocked_urlopen.return_value = response
        gemini.ATTEMPTED_MODELS.clear()
        gemini.USED_MODELS.clear()
        self.assertEqual(generate("system role", "user prompt"), "independent provider ok")
        request = mocked_urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/chat/completions"))
        self.assertEqual(request.get_header("Authorization"), "Bearer openai-test-key")
        self.assertIn("openai:gpt-4.1-mini", gemini.USED_MODELS)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-key", "OPENAI_API_KEY": "openai-key"})
    @patch("agent.gemini.time.sleep")
    @patch("agent.gemini.urllib.request.urlopen")
    def test_independent_provider_is_used_after_all_gemini_models_are_temporarily_unavailable(
        self, mocked_urlopen, _sleep
    ):
        import agent.gemini as gemini
        failures = [
            urllib.error.HTTPError("https://example.invalid", 503, "busy", {}, io.BytesIO(b"busy"))
            for _ in range(6)
        ]
        success = MagicMock()
        success.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": "cross-provider recovery"}}]
        }).encode()
        mocked_urlopen.side_effect = [*failures, success]
        with patch("agent.gemini.MODEL", "primary-model"), patch(
            "agent.gemini.ACTIVE_MODEL", "primary-model"
        ), patch("agent.gemini.FALLBACK_MODELS", ["fallback-model"]):
            self.assertEqual(generate("role", "prompt"), "cross-provider recovery")
        self.assertEqual(mocked_urlopen.call_count, 7)
        self.assertTrue(mocked_urlopen.call_args.args[0].full_url.endswith("/chat/completions"))
        self.assertIn("openai:gpt-4.1-mini", gemini.USED_MODELS)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-key", "OPENAI_API_KEY": "openai-key"})
    @patch("agent.gemini.urllib.request.urlopen")
    def test_non_retryable_gemini_failure_does_not_switch_provider(self, mocked_urlopen):
        mocked_urlopen.side_effect = urllib.error.HTTPError(
            "https://example.invalid", 403, "forbidden", {}, io.BytesIO(b"forbidden")
        )
        with patch("agent.gemini.MODEL", "primary-model"), patch(
            "agent.gemini.ACTIVE_MODEL", "primary-model"
        ), patch("agent.gemini.FALLBACK_MODELS", []):
            with self.assertRaisesRegex(AgentError, "HTTP 403"):
                generate("role", "prompt")
        self.assertEqual(mocked_urlopen.call_count, 1)

    def test_openai_base_url_rejects_non_https_and_embedded_credentials(self):
        from agent.gemini import _openai_endpoint
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "http://example.invalid/v1"}):
            with self.assertRaisesRegex(AgentError, "HTTPS"):
                _openai_endpoint()
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "https://user:pass@example.invalid/v1"}):
            with self.assertRaisesRegex(AgentError, "HTTPS"):
                _openai_endpoint()

    def test_empty_optional_workflow_variables_use_safe_provider_defaults(self):
        from agent.gemini import _openai_endpoint
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "", "OPENAI_MODEL": ""}):
            endpoint, model = _openai_endpoint()
        self.assertEqual(endpoint, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(model, "gpt-4.1-mini")




class IndependentProviderSafetyTests(unittest.TestCase):
    def test_openai_text_content_parts_are_joined_with_real_newlines(self):
        from agent.gemini import _extract_openai_text
        result = _extract_openai_text({"choices": [{"message": {"content": [
            {"type": "text", "text": "first"},
            {"type": "image", "image_url": {}},
            {"type": "text", "text": "second"},
        ]}}]})
        self.assertEqual(result, "first\nsecond")

    @patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-secret", "OPENAI_API_KEY": "openai-secret"})
    def test_main_redacts_both_provider_keys_before_logging_or_persisting(self):
        from contextlib import redirect_stderr
        from io import StringIO
        from agent import run as run_module
        captured = {}
        def capture(error):
            captured["error"] = error
        with patch("agent.run.run_cycle", side_effect=RuntimeError(
                "provider response leaked gemini-secret and openai-secret")), \
             patch("agent.run.write_failure_state", side_effect=capture), \
             redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                run_module.main()
        self.assertNotIn("gemini-secret", captured["error"])
        self.assertNotIn("openai-secret", captured["error"])
        self.assertEqual(captured["error"].count("[REDACTED]"), 2)




class AgentFailureIsolationPolicyTests(unittest.TestCase):
    def test_transient_provider_failure_is_recoverable(self):
        from agent.run import classify_failure
        policy = classify_failure(AgentError("HTTP 503: temporarily unavailable", retryable=True))
        self.assertEqual(policy["category"], "transient_dependency")
        self.assertTrue(policy["retryable"])
        self.assertEqual(policy["strategy"], "checkpoint_resume")

    def test_authentication_failure_is_not_retried_or_isolated(self):
        from agent.run import classify_failure
        policy = classify_failure(AgentError("HTTP 403: forbidden"))
        self.assertEqual(policy["category"], "security_or_permission")
        self.assertFalse(policy["retryable"])
        self.assertEqual(policy["strategy"], "human_review")

    def test_unknown_failure_fails_closed(self):
        from agent.run import classify_failure
        policy = classify_failure(RuntimeError("unexpected parser defect"))
        self.assertEqual(policy["category"], "unknown")
        self.assertFalse(policy["retryable"])

    def test_degraded_run_is_paused_until_recovery(self):
        from agent.resilience import RunStateMachine
        class Journal:
            def __init__(self):
                self.events = []
            def record(self, event, **kwargs):
                self.events.append((event, kwargs))
        journal = Journal()
        machine = RunStateMachine(journal)
        machine.transition("running")
        machine.transition("paused_recovery_required")
        self.assertEqual(machine.state, "paused_recovery_required")
        from agent.resilience import JOURNAL_EVENT_TYPES
        self.assertIn("run_paused", JOURNAL_EVENT_TYPES)
        with self.assertRaisesRegex(ValueError, "Invalid run state transition"):
            machine.transition("completed")


if __name__ == "__main__":
    unittest.main()
