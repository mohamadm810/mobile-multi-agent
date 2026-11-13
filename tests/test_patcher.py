import tempfile
import unittest
from pathlib import Path

from agent.patcher import prepare_patch_spec, apply_unified_diff
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
