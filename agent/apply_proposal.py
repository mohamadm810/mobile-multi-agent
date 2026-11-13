"""Single-use, fingerprint-bound authorization for applying a stored patch proposal."""

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .patcher import apply_patch_spec
from .patch_spec import PatchSpecError, patch_proposal_fingerprint, validate_patch_spec
from .run import atomic_write_text, read_request, repository_fingerprint

ROOT = Path(__file__).resolve().parents[1]
PROPOSAL_FILE = ROOT / "workspace" / "PATCH_PROPOSAL.json"
STATE_FILE = ROOT / "workspace" / "STATE.json"
APPROVAL_LEDGER_FILE = ROOT / "workspace" / "APPROVAL_LEDGER.json"
MAX_APPROVAL_RECORDS = 1000
OWNER = "mohamadm810"


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


def load_approval_ledger():
    """Load a strict append-only ledger; malformed state fails closed."""
    if not APPROVAL_LEDGER_FILE.exists():
        return {"version": 1, "consumed": []}
    try:
        ledger = json.loads(APPROVAL_LEDGER_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise PatchSpecError("The one-time approval ledger is unreadable; manual review is required.") from None
    if (not isinstance(ledger, dict) or set(ledger) != {"version", "consumed"}
            or type(ledger.get("version")) is not int or ledger["version"] != 1
            or not isinstance(ledger.get("consumed"), list)
            or len(ledger["consumed"]) > MAX_APPROVAL_RECORDS):
        raise PatchSpecError("The one-time approval ledger is invalid; refusing to continue.")
    required = {"fingerprint", "run_id", "run_attempt", "actor", "consumed_at"}
    for item in ledger["consumed"]:
        if (not isinstance(item, dict) or set(item) != required
                or not isinstance(item.get("fingerprint"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["fingerprint"])
                or not isinstance(item.get("run_id"), str) or not item["run_id"]
                or type(item.get("run_attempt")) is not int or item["run_attempt"] < 1
                or not isinstance(item.get("actor"), str) or not item["actor"]
                or not isinstance(item.get("consumed_at"), str) or not item["consumed_at"]):
            raise PatchSpecError("The one-time approval ledger contains an invalid record.")
    return ledger


def consume_approval(state, proposal, request, source_fingerprint, *,
                     expected_fingerprint, actor, run_id, run_attempt):
    """Consume a reviewed proposal fingerprint once, bound to a single workflow attempt."""
    validated = verify_proposal_freshness(state, proposal, request, source_fingerprint)
    actual = patch_proposal_fingerprint(validated)
    if not isinstance(expected_fingerprint, str) or expected_fingerprint != actual:
        raise PatchSpecError("The supplied proposal fingerprint does not match the reviewed patch.")
    if not isinstance(actor, str) or not actor or not isinstance(run_id, str) or not run_id:
        raise PatchSpecError("Approval identity is missing.")
    if type(run_attempt) is not int or run_attempt < 1:
        raise PatchSpecError("Workflow attempt identity is invalid.")

    ledger = load_approval_ledger()
    if any(item["fingerprint"] == actual for item in ledger["consumed"]):
        raise PatchSpecError("This proposal approval has already been consumed; generate a fresh proposal.")
    if len(ledger["consumed"]) >= MAX_APPROVAL_RECORDS:
        raise PatchSpecError("Approval ledger capacity reached; manual maintenance is required.")

    ledger["consumed"].append({
        "fingerprint": actual,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "actor": actor,
        "consumed_at": datetime.now(timezone.utc).isoformat(),
    })
    atomic_write_text(APPROVAL_LEDGER_FILE, json.dumps(ledger, ensure_ascii=False, indent=2) + "\n")
    return actual


def require_consumed_approval(fingerprint, *, actor, run_id, run_attempt):
    """Require the exact approval to have been consumed by this workflow attempt."""
    ledger = load_approval_ledger()
    for item in ledger["consumed"]:
        if (item["fingerprint"] == fingerprint and item["actor"] == actor
                and item["run_id"] == run_id and item["run_attempt"] == run_attempt):
            return True
    raise PatchSpecError("No matching one-time approval was consumed by this workflow attempt.")


def main():
    if os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        raise PatchSpecError("Patch application is allowed only from manual workflow dispatch.")
    actor = os.environ.get("GITHUB_ACTOR", "")
    if actor != OWNER:
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
    actual_fingerprint = patch_proposal_fingerprint(validated)
    expected_fingerprint = os.environ.get("PROPOSAL_FINGERPRINT", "")
    if expected_fingerprint != actual_fingerprint:
        raise PatchSpecError("The supplied proposal fingerprint does not match the reviewed patch.")

    run_id = os.environ.get("GITHUB_RUN_ID", "")
    try:
        run_attempt = int(os.environ.get("GITHUB_RUN_ATTEMPT", ""))
    except ValueError:
        raise PatchSpecError("Workflow attempt identity is missing or invalid.") from None

    if os.environ.get("APPROVAL_CONSUME_ONLY") == "1":
        consume_approval(
            state, validated, request, source,
            expected_fingerprint=expected_fingerprint,
            actor=actor, run_id=run_id, run_attempt=run_attempt,
        )
        print("Consumed one-time approval for the exact reviewed proposal.")
        return

    if os.environ.get("APPROVAL_CONSUME_ONLY"):
        raise PatchSpecError("Invalid approval-consumption mode.")
    require_consumed_approval(
        actual_fingerprint, actor=actor, run_id=run_id, run_attempt=run_attempt
    )
    changed = apply_patch_spec(ROOT, validated, authorization_phrase=authorization)
    for path in changed:
        print(f"Prepared approved change: {path}")


if __name__ == "__main__":
    main()
