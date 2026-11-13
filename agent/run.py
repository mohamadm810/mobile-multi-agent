import ast
import json
import os
import re
import sys
import tempfile
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
MAX_REPOSITORY_CONTEXT_CHARS = 40000


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


def get_unit_test_inventory():
    """Return exact unittest method names found in the checked-in test source."""
    test_file = ROOT / "tests" / "test_agent.py"
    try:
        tree = ast.parse(test_file.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        return []
    names = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            names.append(node.name)
    return sorted(set(names))


def atomic_write_text(path, content):
    """Atomically replace a UTF-8 text file using a temp file in the same directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


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
    unit_test_methods = get_unit_test_inventory()
    context += (
        "\n\nDETERMINISTIC TEST INVENTORY: Exactly "
        + str(len(unit_test_methods)) + " unittest methods are present in tests/test_agent.py. "
        + "Names: " + ", ".join(unit_test_methods) + ". "
        + "Use this exact count and list; do not estimate, omit tests, or claim the test file is absent."
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
                + ". This is a live API run, not a mocked simulation. Source contains "
                + str(len(unit_test_methods)) + " unittest methods: " + ", ".join(unit_test_methods)
                + ". The separate GitHub Tests workflow result is not available inside this Python process; do not guess it."
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
    for role_name, role_output in outputs.items():
        role_output = re.sub(
            r"\b\d+\s+unit test methods?\b",
            f"{len(unit_test_methods)} unit test methods",
            role_output,
            flags=re.IGNORECASE,
        )
        role_output = re.sub(
            r"\b\d+\s+unit tests\b",
            f"{len(unit_test_methods)} unit tests",
            role_output,
            flags=re.IGNORECASE,
        )
        outputs[role_name] = role_output
    models_used = list(USED_MODELS)
    models_attempted = list(ATTEMPTED_MODELS)
    report = [
        "# گزارش اجرای تیم ایجنت",
        f"\nزمان اجرا (UTC): `{now}`",
        f"\nشناسه اجرای GitHub: `{__import__('os').getenv('GITHUB_RUN_ID', 'local')}`",
        f"\nنسخه سورس اجراشده: `{__import__('os').getenv('GITHUB_SHA', 'local/unknown')}`",
        f"\nتعداد متدهای تست موجود در سورس: `{len(unit_test_methods)}`",
        "\n> این چرخه تحلیل و برنامه‌ریزی است؛ اجرای تست‌های پروژه در گردش‌کار مستقل Tests انجام می‌شود.",
        f"\nمدل(های) تلاش‌شده: `{', '.join(models_attempted) or 'ثبت نشده'}`",
        f"\nمدل(های) پاسخ‌گوی موفق: `{', '.join(models_used) or 'ثبت نشده'}`",
    ]
    for name, output in outputs.items():
        report.extend([f"\n\n---\n\n## {name}\n\n{output}"])

    WORKSPACE.mkdir(parents=True, exist_ok=True)
    atomic_write_text(REPORT_FILE, "\n".join(report) + "\n")
    new_state = {
        "last_run_utc": now,
        "request_snapshot": request[:5000],
        "status": "planning_review_required",
        "last_report": "workspace/REPORT.md",
        "completed_agents": list(outputs.keys()),
        "models_used": models_used,
        "models_attempted": models_attempted,
        "unit_test_methods": unit_test_methods,
        "github_run_id": __import__("os").getenv("GITHUB_RUN_ID"),
        "github_sha": __import__("os").getenv("GITHUB_SHA"),
        "next_step": "Review the report, approve decisions, update REQUEST.md, then run again.",
    }
    atomic_write_text(STATE_FILE, json.dumps(new_state, ensure_ascii=False, indent=2) + "\n")
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
    atomic_write_text(STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def main():
    try:
        run_cycle()
    except Exception as exc:
        safe_message = str(exc)
        api_key = __import__("os").getenv("GEMINI_API_KEY", "").strip()
        if api_key:
            safe_message = safe_message.replace(api_key, "[REDACTED]")
        safe_error = f"{type(exc).__name__}: {safe_message}"
        try:
            write_failure_state(safe_error)
        except OSError as state_error:
            print(f"Could not persist failure state: {type(state_error).__name__}", file=sys.stderr)
        print(f"Agent run failed: {safe_error[:1500]}", file=sys.stderr)
        raise SystemExit(1)
    print("Agent run completed. Review workspace/REPORT.md.")


if __name__ == "__main__":
    main()
