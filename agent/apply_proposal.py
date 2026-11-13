"""One-run GitHub Actions authorization gate for applying a stored patch proposal."""

import hashlib
import json
import os
from pathlib import Path

from .patcher import apply_patch_spec
from .patch_spec import PatchSpecError, patch_proposal_fingerprint, validate_patch_spec
from .run import read_request, repository_fingerprint

ROOT = Path(__file__).resolve().parents[1]
PROPOSAL_FILE = ROOT / "workspace" / "PATCH_PROPOSAL.json"
STATE_FILE = ROOT / "workspace" / "STATE.json"


def verify_proposal_freshness(state, proposal, request, source_fingerprint):
    """Require a successful run and exact request, source, and proposal fingerprints."""
    if not isinstance(state, dict) or state.get("status") != "planning_review_required":
        raise PatchSpecError("The latest multi-agent run has not completed successfully.")
    request_fingerprint = hashlib.sha256(request.encode("utf-8")).hexdigest()
    if state.get("request_fingerprint") != request_fingerprint:
        raise PatchSpecError("The stored proposal belongs to a different user request.")
    if state.get("source_fingerprint") != source_fingerprint:
        raise PatchSpecError("The repository source changed after this proposal was generated.")
    status = state.get("patch_proposal_status", {})
    if not isinstance(status, dict) or status.get("status") != "validated":
        raise PatchSpecError("The latest run does not contain a validated patch proposal.")
    validated = validate_patch_spec(proposal)
    if status.get("proposal_fingerprint") != patch_proposal_fingerprint(validated):
        raise PatchSpecError("The stored patch proposal changed after it was reviewed.")
    return validated


def main():
    if os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        raise PatchSpecError("Patch application is allowed only from manual workflow dispatch.")
    if os.environ.get("GITHUB_ACTOR") != "mohamadm810":
        raise PatchSpecError("Only the repository owner may authorize patch application.")
    if os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise PatchSpecError("Patch application must be dispatched from main.")
    authorization = os.environ.get("PATCH_AUTHORIZATION", "")
    if authorization != "APPLY_TO_BRANCH":
        raise PatchSpecError("The explicit APPLY_TO_BRANCH choice is required.")
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        proposal = json.loads(PROPOSAL_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise PatchSpecError("The latest run state or stored patch proposal is missing or invalid.") from None
    request = read_request()
    source = repository_fingerprint()
    validated = verify_proposal_freshness(state, proposal, request, source)
    changed = apply_patch_spec(ROOT, validated, authorization_phrase=authorization)
    for path in changed:
        print(f"Prepared approved change: {path}")


if __name__ == "__main__":
    main()
