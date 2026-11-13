import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.gemini import AgentError
from agent import run as run_module


class AgentStageRecoveryTests(unittest.TestCase):
    def test_optional_role_failure_isolated_then_resumed_from_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            request_file = workspace / "REQUEST.md"
            request_file.write_text("Build a small verified feature.", encoding="utf-8")
            state_file = workspace / "STATE.json"
            report_file = workspace / "REPORT.md"
            team_file = workspace / "TEAM.json"
            calls = []
            fail_tester_once = {"value": True}

            def generator(role, prompt):
                calls.append(role)
                if role == run_module.TESTER and fail_tester_once["value"]:
                    fail_tester_once["value"] = False
                    raise AgentError("temporary network failure", retryable=True)
                if role == run_module.DEVELOPER:
                    return "Implementation plan.\\nPATCH_PROPOSAL_JSON:\\nnone"
                if role == run_module.HR:
                    return json.dumps({
                        "summary": "No staffing changes are justified.",
                        "manager_review": "none",
                        "actions": [],
                    })
                return f"Verified output for {role}."

            registry = {
                "version": 1,
                "custom_agents": [],
                "archived_agents": [],
                "role_overrides": {},
                "disabled_agents": [],
            }
            patches = [
                patch.object(run_module, "WORKSPACE", workspace),
                patch.object(run_module, "REQUEST_FILE", request_file),
                patch.object(run_module, "STATE_FILE", state_file),
                patch.object(run_module, "REPORT_FILE", report_file),
                patch.object(run_module, "TEAM_FILE", team_file),
                patch.object(run_module, "read_repository_context", return_value=""),
                patch.object(run_module, "repository_fingerprint", return_value="stable-source"),
                patch.object(run_module, "get_unit_test_inventory", return_value=["tests/test_example.py:test_ok"]),
                patch.object(run_module, "read_team_registry", return_value=registry.copy()),
                patch.object(run_module, "reset_run_tracking"),
            ]
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9]:
                run_module.run_cycle(generator=generator)
                first_state = json.loads(state_file.read_text(encoding="utf-8"))
                checkpoint_file = workspace / "CHECKPOINT.json"
                checkpoint = json.loads(checkpoint_file.read_text(encoding="utf-8"))

                self.assertEqual(first_state["status"], "completed_with_degraded_roles")
                self.assertIn("Independent Tester", first_state["degraded_agents"])
                self.assertTrue(first_state["checkpoint_available"])
                self.assertIn("Requirements Analyst", checkpoint["outputs"])
                self.assertIn("Developer Planner", checkpoint["outputs"])
                self.assertNotIn("Independent Reviewer", checkpoint["outputs"])
                self.assertNotIn("Central Supervisor", checkpoint["outputs"])
                self.assertIn("Independent Tester", report_file.read_text(encoding="utf-8"))

                first_call_count = len(calls)
                run_module.run_cycle(generator=generator)
                second_state = json.loads(state_file.read_text(encoding="utf-8"))

                self.assertEqual(second_state["status"], "planning_review_required")
                self.assertEqual(second_state["degraded_agents"], [])
                self.assertFalse((workspace / "CHECKPOINT.json").exists())
                self.assertEqual(calls[first_call_count], run_module.TESTER)
                self.assertNotIn(run_module.ANALYST, calls[first_call_count:])
                self.assertNotIn(run_module.ARCHITECT, calls[first_call_count:])
                self.assertNotIn(run_module.DEVELOPER, calls[first_call_count:])
                self.assertIn(run_module.HR, calls[first_call_count:])


if __name__ == "__main__":
    unittest.main()
