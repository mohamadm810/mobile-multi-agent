"""One-run GitHub Actions authorization gate for applying a stored patch proposal."""

import json
import os
from pathlib import Path

from .patcher import apply_patch_spec
from .patch_spec import PatchSpecError

ROOT = Path(__file__).resolve().parents[1]
PROPOSAL_FILE = ROOT / "workspace" / "PATCH_PROPOSAL.json"


def main():
    if os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        raise PatchSpecError("Patch application is allowed only from manual workflow dispatch.")
    if os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise PatchSpecError("Patch application must be dispatched from main.")
    authorization = os.environ.get("PATCH_AUTHORIZATION", "")
    if authorization != "APPLY_TO_BRANCH":
        raise PatchSpecError("The explicit APPLY_TO_BRANCH choice is required.")
    if not PROPOSAL_FILE.is_file():
        raise PatchSpecError("No validated workspace/PATCH_PROPOSAL.json is available.")
    try:
        proposal = json.loads(PROPOSAL_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise PatchSpecError("Stored patch proposal cannot be read as valid JSON.") from None
    changed = apply_patch_spec(ROOT, proposal, authorization_phrase=authorization)
    for path in changed:
        print(f"Prepared approved change: {path}")


if __name__ == "__main__":
    main()
