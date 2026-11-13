import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import MAX_REQUEST_CHARS
from .gemini import AgentError, USED_MODELS, generate
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
    "README_FA.md",
]
MAX_REPOSITORY_CONTEXT_CHARS = 28000


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
        chunk = header + source[:remaining]
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
    for name, role in STAGES:
        prior = "\n\n".join(f"## {key}\n{value}" for key, value in outputs.items())
        prompt = context + "\n\nPREVIOUS AGENT OUTPUTS:\n" + prior[-22000:]
        outputs[name] = generator(role, prompt)

    now = datetime.now(timezone.utc).isoformat()
    report = [
        "# گزارش اجرای تیم ایجنت",
        f"\nزمان اجرا (UTC): `{now}`",
        "\n> این گزارش تحلیل و برنامه‌ریزی مدل است؛ اجرای واقعی کد یا تست پروژه انجام نشده است.",
        f"\nمدل(های) استفاده‌شده: `{', '.join(USED_MODELS) or __import__('os').getenv('GEMINI_MODEL', 'gemini-3.5-flash-lite')}`",
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
