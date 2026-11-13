import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import MAX_REQUEST_CHARS
from .gemini import AgentError, ATTEMPTED_MODELS, USED_MODELS, generate, reset_run_tracking
from .roles import ANALYST, ARCHITECT, DEVELOPER, DOCS, MANAGER, REVIEWER, TESTER

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "workspace"
REQUEST_FILE = WORKSPACE / "REQUEST.md"
STATE_FILE = WORKSPACE / "STATE.json"
REPORT_FILE = WORKSPACE / "REPORT.md"

STAGES = [
    ("Requirements Analyst", ANALYST),
    ("Software Architect", ARCHITECT),
    ("Developer Planner", DEVELOPER),
    ("Independent Tester", TESTER),
    ("Independent Reviewer", REVIEWER),
    ("Central Supervisor", MANAGER),
    ("Documentation Agent", DOCS),
]


def read_request():
    if not REQUEST_FILE.is_file():
        raise AgentError("workspace/REQUEST.md does not exist.")
    request = REQUEST_FILE.read_text(encoding="utf-8").strip()
    if not request or request.startswith("<!--") or len(request) > MAX_REQUEST_CHARS:
        raise AgentError(
            f"Request is empty/template-only or exceeds {MAX_REQUEST_CHARS} characters."
        )
    return request




REPOSITORY_EVIDENCE_FILES = [
    "agent/config.py",
    "agent/gemini.py",
    "agent/run.py",
    "agent/roles.py",
    ".github/workflows/run-agent.yml",
    "tests/test_agent.py",
]
MAX_REPOSITORY_CONTEXT_CHARS = 24000


def read_repository_context():
    """Return bounded source evidence so agents can audit implementation claims."""
    sections = []
    remaining = MAX_REPOSITORY_CONTEXT_CHARS
    for relative_path in REPOSITORY_EVIDENCE_FILES:
        path = ROOT / relative_path
        if not path.is_file() or remaining <= 0:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        header = f"\n\n### REPOSITORY FILE: {relative_path}\n"
        chunk = header + source
        if len(chunk) > remaining:
            continue
        sections.append(chunk)
        remaining -= len(chunk)
    return "\n".join(sections)


def read_state():
    if not STATE_FILE.exists():
        return {"status": "not_started"}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {"warning": "State was not a JSON object."}
    except (OSError, json.JSONDecodeError):
        return {"warning": "Previous state file was invalid; starting a fresh state."}


def run_cycle(request=None, generator=generate):
    reset_run_tracking()
    request = read_request() if request is None else request.strip()
    if not request or len(request) > MAX_REQUEST_CHARS:
        raise AgentError("Request is empty or exceeds the configured size limit.")

    state = read_state()
    repository_context = read_repository_context()
    context = (
        "USER REQUEST:\n" + request
        + "\n\nPREVIOUS PROJECT STATE:\n"
        + json.dumps(state, ensure_ascii=False)[:12000]
        + "\n\nVERIFIABLE REPOSITORY SOURCE EVIDENCE:\n"
        + repository_context
        + "\n\nEVIDENCE RULE: Treat the source files above as the authority for current implementation. "
        "Do not call implemented code proposed, and do not claim unimplemented behavior exists. "
        "Distinguish source-level implementation, unit-test simulation, and behavior observed in this live run."
    )
    outputs = {}
    prior_for_stage = {
        "Requirements Analyst": [],
        "Software Architect": ["Requirements Analyst"],
        "Developer Planner": ["Requirements Analyst", "Software Architect"],
        "Independent Tester": ["Requirements Analyst", "Developer Planner"],
        "Independent Reviewer": ["Requirements Analyst", "Software Architect", "Developer Planner", "Independent Tester"],
        "Central Supervisor": ["Requirements Analyst", "Software Architect", "Developer Planner", "Independent Tester", "Independent Reviewer"],
        "Documentation Agent": ["Software Architect", "Independent Tester", "Independent Reviewer", "Central Supervisor"],
    }
    for name, role in STAGES:
        selected = prior_for_stage[name]
        prior = "\n\n".join(
            f"## {key}\n{outputs[key][:3500]}" for key in selected if key in outputs
        )
        runtime_evidence = ""
        if name == "Documentation Agent":
            runtime_evidence = (
                "\n\nLIVE EXECUTION EVIDENCE: The preceding six role calls in this cycle "
                "returned successfully from the Gemini API. Successful response model(s): "
                + (", ".join(USED_MODELS) or "none recorded")
                + ". Attempted model(s): " + (", ".join(ATTEMPTED_MODELS) or "none recorded")
                + ". This is a live API run, not a mocked simulation. The separate GitHub Tests "
                  "workflow result is not available inside this Python process; do not guess it."
            )
        prompt = (
            context
            + "\n\nYOUR ROLE FOR THIS STEP: " + name
            + "\nReturn only your role's deliverable. Do not write sections for other roles, "
              "do not copy prior outputs, and obey the word limit in your role instructions."
            + ("\n\nRELEVANT PRIOR FINDINGS (reference only; do not repeat):\n" + prior if prior else "")
            + runtime_evidence
        )
        outputs[name] = generator(role, prompt)

    now = datetime.now(timezone.utc).isoformat()
    models_used = list(USED_MODELS)
    models_attempted = list(ATTEMPTED_MODELS)
    report = [
        "# گزارش اجرای تیم ایجنت",
        f"\nزمان اجرا (UTC): `{now}`",
        f"\nشناسه اجرای GitHub: `{__import__('os').getenv('GITHUB_RUN_ID', 'local')}`",
        f"\nنسخه سورس اجراشده: `{__import__('os').getenv('GITHUB_SHA', 'local/unknown')}`",
        "\n> این چرخه تحلیل و برنامه‌ریزی است؛ اجرای تست‌های پروژه در گردش‌کار مستقل Tests انجام می‌شود.",
        f"\nمدل(های) تلاش‌شده: `{', '.join(models_attempted) or 'ثبت نشده'}`",
        f"\nمدل(های) پاسخ‌گوی موفق: `{', '.join(models_used) or 'ثبت نشده'}`",
    ]
    for name, output in outputs.items():
        report.extend([f"\n\n---\n\n## {name}\n\n{output}"])

    WORKSPACE.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text("\n".join(report) + "\n", encoding="utf-8")
    new_state = {
        "last_run_utc": now,
        "request_snapshot": request[:5000],
        "status": "planning_review_required",
        "last_report": "workspace/REPORT.md",
        "completed_agents": list(outputs.keys()),
        "models_used": models_used,
        "models_attempted": models_attempted,
        "github_run_id": __import__("os").getenv("GITHUB_RUN_ID"),
        "github_sha": __import__("os").getenv("GITHUB_SHA"),
        "next_step": "Review the report, approve decisions, update REQUEST.md, then run again.",
    }
    STATE_FILE.write_text(json.dumps(new_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return outputs



def write_failure_state(error):
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    state = read_state()
    state.update({
        "status": "failed",
        "last_run_utc": datetime.now(timezone.utc).isoformat(),
        "last_error": str(error)[:1500],
        "next_step": "Review the Actions log, resolve the reported issue, then rerun the workflow.",
    })
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    try:
        run_cycle()
    except AgentError as exc:
        try:
            write_failure_state(exc)
        except OSError as state_error:
            print(f"Could not persist failure state: {state_error}", file=sys.stderr)
        print(f"Agent run failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print("Agent run completed. Review workspace/REPORT.md.")


if __name__ == "__main__":
    main()
