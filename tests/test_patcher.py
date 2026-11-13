import tempfile
import unittest
from pathlib import Path

from agent.patcher import prepare_patch_spec, apply_unified_diff, apply_patch_spec
from agent.patch_spec import PatchSpecError, validate_patch_spec


def proposal(path="agent/example.py", diff=None):
    if diff is None:
        diff = (
            f"--- a/{path}\n+++ b/{path}\n"
            "@@ -1,2 +1,2 @@\n keep\n-old\n+new\n"
        )
    return {"version": 1, "changes": [{"path": path, "unified_diff": diff}]}


class PatchSpecValidationTests(unittest.TestCase):
    def test_accepts_valid_single_file_proposal(self):
        checked = validate_patch_spec(proposal())
        self.assertEqual(checked["changes"][0]["path"], "agent/example.py")

    def test_accepts_json_string(self):
        import json
        self.assertEqual(validate_patch_spec(json.dumps(proposal()))["version"], 1)

    def test_rejects_invalid_json(self):
        with self.assertRaisesRegex(PatchSpecError, "valid JSON"):
            validate_patch_spec("{invalid")

    def test_rejects_unknown_top_level_fields(self):
        data = proposal()
        data["approved"] = True
        with self.assertRaisesRegex(PatchSpecError, "only version and changes"):
            validate_patch_spec(data)

    def test_rejects_boolean_version(self):
        data = proposal()
        data["version"] = True
        with self.assertRaisesRegex(PatchSpecError, "version"):
            validate_patch_spec(data)

    def test_rejects_path_traversal_and_absolute_paths(self):
        for path in ("../outside.py", "agent/../../outside.py", "/tmp/outside.py", "agent\\outside.py"):
            with self.subTest(path=path), self.assertRaises(PatchSpecError):
                validate_patch_spec(proposal(path=path))

    def test_rejects_non_python_files_inside_allowed_directories(self):
        with self.assertRaisesRegex(PatchSpecError, "Python source"):
            validate_patch_spec(proposal(path="agent/config.json"))

    def test_rejects_protected_orchestration_files(self):
        for path in ("agent/run.py", "agent/patch_spec.py", "agent/gemini.py"):
            with self.subTest(path=path), self.assertRaisesRegex(PatchSpecError, "protected"):
                validate_patch_spec(proposal(path=path))

    def test_rejects_paths_outside_allowlist(self):
        with self.assertRaisesRegex(PatchSpecError, "agent/ or tests/"):
            validate_patch_spec(proposal(path=".github/workflows/tests.yml"))

    def test_rejects_header_path_tampering(self):
        diff = "--- a/agent/example.py\n+++ b/tests/other.py\n@@ -1 +1 @@\n-old\n+new\n"
        with self.assertRaisesRegex(PatchSpecError, "headers"):
            validate_patch_spec(proposal(diff=diff))

    def test_rejects_git_metadata_and_binary_patch(self):
        diff = (
            "diff --git a/agent/example.py b/agent/example.py\n"
            "--- a/agent/example.py\n+++ b/agent/example.py\n"
            "@@ -1 +1 @@\n-old\n+new\n"
        )
        with self.assertRaisesRegex(PatchSpecError, "metadata"):
            validate_patch_spec(proposal(diff=diff))

    def test_rejects_duplicate_paths(self):
        data = proposal()
        data["changes"].append(data["changes"][0])
        with self.assertRaisesRegex(PatchSpecError, "once"):
            validate_patch_spec(data)


class UnifiedDiffTests(unittest.TestCase):
    def test_applies_exact_context_hunk(self):
        diff = "--- a/agent/example.py\n+++ b/agent/example.py\n@@ -1,2 +1,2 @@\n keep\n-old\n+new\n"
        self.assertEqual(apply_unified_diff("keep\nold\n", diff, "agent/example.py"), "keep\nnew\n")

    def test_rejects_context_mismatch(self):
        diff = "--- a/agent/example.py\n+++ b/agent/example.py\n@@ -1 +1 @@\n-old\n+new\n"
        with self.assertRaisesRegex(PatchSpecError, "does not match"):
            apply_unified_diff("different\n", diff, "agent/example.py")

    def test_rejects_hunk_count_mismatch(self):
        diff = "--- a/agent/example.py\n+++ b/agent/example.py\n@@ -1,2 +1,1 @@\n-old\n+new\n"
        with self.assertRaisesRegex(PatchSpecError, "counts"):
            apply_unified_diff("old\n", diff, "agent/example.py")

    def test_rejects_inconsistent_new_side_hunk_coordinate(self):
        diff = "--- a/agent/example.py\n+++ b/agent/example.py\n@@ -1 +2 @@\n-old\n+new\n"
        with self.assertRaisesRegex(PatchSpecError, "New-side hunk coordinates"):
            apply_unified_diff("old\n", diff, "agent/example.py")

    def test_applies_insertion_at_file_start(self):
        diff = "--- a/agent/example.py\n+++ b/agent/example.py\n@@ -0,0 +1 @@\n+first\n"
        self.assertEqual(apply_unified_diff("second\n", diff, "agent/example.py"), "first\nsecond\n")


class ProposalFreshnessTests(unittest.TestCase):
    def setUp(self):
        from agent.patch_spec import patch_proposal_fingerprint
        from agent.apply_proposal import verify_proposal_freshness

        self.proposal = proposal()
        self.request = "implement the requested change"
        self.source = "source-fingerprint"
        self.state = {
            "status": "planning_review_required",
            "request_fingerprint": __import__("hashlib").sha256(self.request.encode("utf-8")).hexdigest(),
            "source_fingerprint": self.source,
            "patch_proposal_status": {
                "status": "validated",
                "proposal_fingerprint": patch_proposal_fingerprint(self.proposal),
            },
        }
        self.verify = verify_proposal_freshness

    def test_accepts_successful_fresh_proposal(self):
        validated = self.verify(self.state, self.proposal, self.request, self.source)
        self.assertEqual(validated["version"], 1)

    def test_rejects_failed_or_incomplete_run(self):
        state = dict(self.state, status="failed")
        with self.assertRaisesRegex(PatchSpecError, "not completed successfully"):
            self.verify(state, self.proposal, self.request, self.source)

    def test_rejects_changed_user_request(self):
        with self.assertRaisesRegex(PatchSpecError, "different user request"):
            self.verify(self.state, self.proposal, "new request", self.source)

    def test_rejects_changed_repository_source(self):
        with self.assertRaisesRegex(PatchSpecError, "source changed"):
            self.verify(self.state, self.proposal, self.request, "new-source")

    def test_rejects_proposal_tampering(self):
        altered = proposal()
        altered["changes"][0]["unified_diff"] = "\\n".join([
            "--- a/agent/example.py",
            "+++ b/agent/example.py",
            "@@ -1 +1 @@",
            "-old",
            "+altered",
            "",
        ])
        with self.assertRaisesRegex(PatchSpecError, "changed after it was reviewed"):
            self.verify(self.state, altered, self.request, self.source)


class PatchPreparationTests(unittest.TestCase):
    def test_prepares_patch_without_mutating_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agent").mkdir()
            target = root / "agent" / "example.py"
            target.write_text("keep\nold\n", encoding="utf-8")
            prepared = prepare_patch_spec(root, proposal())
            self.assertEqual(prepared, {"agent/example.py": "keep\nnew\n"})
            self.assertEqual(target.read_text(encoding="utf-8"), "keep\nold\n")

    def test_refuses_patch_application_without_explicit_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agent").mkdir()
            target = root / "agent" / "example.py"
            target.write_text("keep\nold\n", encoding="utf-8")
            with self.assertRaisesRegex(PatchSpecError, "authorization"):
                apply_patch_spec(root, proposal(), authorization_phrase="REVIEW_ONLY")
            self.assertEqual(target.read_text(encoding="utf-8"), "keep\nold\n")

    def test_applies_patch_after_explicit_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agent").mkdir()
            target = root / "agent" / "example.py"
            target.write_text("keep\nold\n", encoding="utf-8")
            changed = apply_patch_spec(root, proposal(), authorization_phrase="APPLY_TO_BRANCH")
            self.assertEqual(changed, ["agent/example.py"])
            self.assertEqual(target.read_text(encoding="utf-8"), "keep\nnew\n")

    def test_persist_patch_proposal_accepts_only_validated_json(self):
        import json
        from agent.run import persist_patch_proposal

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "PATCH_PROPOSAL.json"
            output = "Plan complete.\nPATCH_PROPOSAL_JSON:\n" + json.dumps(proposal())
            status = persist_patch_proposal(output, path)
            self.assertEqual(status["status"], "validated")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 1)

    def test_persist_patch_proposal_rejects_unsafe_output_and_clears_stale_file(self):
        from agent.run import persist_patch_proposal

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "PATCH_PROPOSAL.json"
            path.write_text("stale", encoding="utf-8")
            unsafe = "PATCH_PROPOSAL_JSON:\n" + __import__("json").dumps(proposal(path="../outside.py"))
            status = persist_patch_proposal(unsafe, path)
            self.assertEqual(status["status"], "rejected")
            self.assertFalse(path.exists())

    def test_apply_proposal_requires_manual_dispatch_from_main(self):
        import os
        from unittest.mock import patch
        from agent.apply_proposal import main

        with patch.dict(os.environ, {
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_ACTOR": "mohamadm810",
            "GITHUB_REF": "refs/heads/main",
            "PATCH_AUTHORIZATION": "APPLY_TO_BRANCH",
        }, clear=True):
            with self.assertRaisesRegex(PatchSpecError, "manual workflow dispatch"):
                main()

    def test_apply_proposal_rejects_non_owner_even_with_dispatch_choice(self):
        import os
        from unittest.mock import patch
        from agent.apply_proposal import main

        with patch.dict(os.environ, {
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_ACTOR": "other-collaborator",
            "GITHUB_REF": "refs/heads/main",
            "PATCH_AUTHORIZATION": "APPLY_TO_BRANCH",
        }, clear=True):
            with self.assertRaisesRegex(PatchSpecError, "repository owner"):
                main()

    def test_apply_proposal_rejects_dispatch_from_non_main_branch(self):
        import os
        from unittest.mock import patch
        from agent.apply_proposal import main

        with patch.dict(os.environ, {
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_ACTOR": "mohamadm810",
            "GITHUB_REF": "refs/heads/feature/other",
            "PATCH_AUTHORIZATION": "APPLY_TO_BRANCH",
        }, clear=True):
            with self.assertRaisesRegex(PatchSpecError, "from main"):
                main()

    def test_apply_proposal_rejects_review_only_choice(self):
        import os
        from unittest.mock import patch
        from agent.apply_proposal import main

        with patch.dict(os.environ, {
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_ACTOR": "mohamadm810",
            "GITHUB_REF": "refs/heads/main",
            "PATCH_AUTHORIZATION": "REVIEW_ONLY",
        }, clear=True):
            with self.assertRaisesRegex(PatchSpecError, "APPLY_TO_BRANCH"):
                main()

    def test_rejects_missing_target_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(PatchSpecError, "existing file"):
                prepare_patch_spec(directory, proposal())

    def test_rejects_symlink_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agent").mkdir()
            outside = root / "outside.py"
            outside.write_text("keep\nold\n", encoding="utf-8")
            (root / "agent" / "example.py").symlink_to(outside)
            with self.assertRaisesRegex(PatchSpecError, "Symbolic-link"):
                prepare_patch_spec(root, proposal())


if __name__ == "__main__":
    unittest.main()
