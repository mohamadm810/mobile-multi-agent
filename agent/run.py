import ast
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .config import MAX_REQUEST_CHARS
from .gemini import AgentError, ATTEMPTED_MODELS, USED_MODELS, generate, reset_run_tracking
from .roles import ANALYST, ARCHITECT, DEVELOPER, DOCS, HR, MANAGER, REVIEWER, TESTER
from .resilience import CheckpointStore, call_with_retries

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "workspace"
REQUEST_FILE = WORKSPACE / "REQUEST.md"
STATE_FILE = WORKSPACE / "STATE.json"
REPORT_FILE = WORKSPACE / "REPORT.md"
TEAM_FILE = WORKSPACE / "TEAM.json"
MAX_CUSTOM_AGENTS = 5
MAX_STAFFING_ACTIONS = 2

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
    raw_request = REQUEST_FILE.read_text(encoding="utf-8").strip()
    if not raw_request or len(raw_request) > MAX_REQUEST_CHARS:
        raise AgentError(
            f"Request is empty/template-only or exceeds {MAX_REQUEST_CHARS} characters."
        )
    request = re.sub(r"<!--.*?-->", "", raw_request, flags=re.DOTALL).strip()
    if not request:
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
MAX_REPOSITORY_CONTEXT_CHARS = 65000


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


def repository_fingerprint():
    """Hash source evidence so checkpoints never cross code revisions."""
    digest = hashlib.sha256()
    for relative_path in REPOSITORY_EVIDENCE_FILES:
        path = ROOT / relative_path
        if not path.is_file():
            continue
        try:
            content = path.read_bytes()
        except OSError:
            continue
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\\0")
        digest.update(content)
        digest.update(b"\\0")
    return digest.hexdigest()


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



CORE_AGENT_NAMES = {name for name, _ in STAGES} | {"Agent Recruitment & Governance"}


def read_team_registry():
    """Load only validated custom-agent definitions; never override protected core roles."""
    if not TEAM_FILE.is_file():
        return {"version": 1, "custom_agents": [], "archived_agents": [], "role_overrides": {}, "disabled_agents": []}
    try:
        data = json.loads(TEAM_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "custom_agents": [], "archived_agents": [], "role_overrides": {}, "disabled_agents": []}
    if not isinstance(data, dict) or not isinstance(data.get("custom_agents"), list):
        return {"version": 1, "custom_agents": [], "archived_agents": [], "role_overrides": {}, "disabled_agents": []}
    active, seen = [], set()
    for item in data["custom_agents"]:
        if not isinstance(item, dict):
            continue
        name, prompt, mission = item.get("name"), item.get("prompt"), item.get("mission")
        if isinstance(name, str):
            name = name.strip()
        if (not isinstance(name, str) or not isinstance(prompt, str)
                or not isinstance(mission, str) or name in CORE_AGENT_NAMES
                or not 3 <= len(name.strip()) <= 60 or not 50 <= len(prompt) <= 4000
                or not 3 <= len(mission.strip()) <= 300 or name in seen):
            continue
        seen.add(name)
        active.append({"name": name.strip(), "mission": mission.strip(), "prompt": prompt})
    data["version"] = 1
    data["custom_agents"] = active[:MAX_CUSTOM_AGENTS]
    overrides = data.get("role_overrides", {})
    if not isinstance(overrides, dict):
        overrides = {}
    data["role_overrides"] = {key: value for key, value in overrides.items()
                              if key in CORE_AGENT_NAMES - {"Central Supervisor", "Agent Recruitment & Governance"}
                              and isinstance(value, str) and 50 <= len(value) <= 4000}
    disabled = data.get("disabled_agents", [])
    data["disabled_agents"] = sorted({name for name in disabled if isinstance(name, str)
                                      and name in CORE_AGENT_NAMES - {"Central Supervisor", "Agent Recruitment & Governance"}}) if isinstance(disabled, list) else []
    archived = data.get("archived_agents", [])
    data["archived_agents"] = [item for item in archived if isinstance(item, dict)][-50:] if isinstance(archived, list) else []
    return data


def _extract_hr_json(output):
    """Parse strict JSON from the HR agent, allowing a single fenced JSON response."""
    text = output.strip()
    if text.startswith("```"):
        text = re.sub(r"^\`\`\`(?:json)?\s*|\s*\`\`\`$", "", text, flags=re.IGNORECASE)
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("actions", []), list):
        raise ValueError("HR response must be a JSON object with an actions array.")
    return data


def apply_hr_decisions(output, registry):
    """Apply bounded HR changes to custom agents only; manager/core roles are immutable."""
    decision = _extract_hr_json(output)
    actions = decision.get("actions", [])
    if len(actions) > MAX_STAFFING_ACTIONS:
        raise ValueError("HR exceeded the per-run staffing action limit.")
    applied, archived = [], registry.setdefault("archived_agents", [])
    active = registry.setdefault("custom_agents", [])
    overrides = registry.setdefault("role_overrides", {})
    disabled = registry.setdefault("disabled_agents", [])
    for action in actions:
        if not isinstance(action, dict):
            raise ValueError("Malformed HR staffing action.")
        kind, name = action.get("action"), action.get("name")
        if isinstance(name, str):
            name = name.strip()
        if not isinstance(name, str) or name in {"Central Supervisor", "Agent Recruitment & Governance"}:
            raise ValueError("HR cannot change the Central Supervisor or its own role.")
        existing = next((agent for agent in active if agent["name"] == name), None)
        if kind == "revise":
            prompt = action.get("prompt")
            if not isinstance(prompt, str) or not 50 <= len(prompt) <= 4000:
                raise ValueError("A revised agent prompt must contain 50 to 4000 characters.")
            if existing:
                existing["prompt"] = prompt
                if isinstance(action.get("mission"), str) and 3 <= len(action["mission"].strip()) <= 300:
                    existing["mission"] = action["mission"].strip()
            elif name in CORE_AGENT_NAMES:
                overrides[name] = prompt
            else:
                raise ValueError("Only an active agent can be revised.")
            applied.append(f"Updated role instructions for {name}; effective next cycle.")
        elif kind == "hire":
            if name in CORE_AGENT_NAMES:
                raise ValueError("HR cannot create a duplicate of a protected core role.")
            mission, prompt = action.get("mission"), action.get("prompt")
            if existing or any(x.get("name") == name for x in archived):
                raise ValueError("Duplicate or previously retired custom-agent name.")
            if len(active) >= MAX_CUSTOM_AGENTS:
                raise ValueError("Maximum active custom-agent count reached.")
            if (not isinstance(mission, str) or not 3 <= len(mission.strip()) <= 300
                    or not isinstance(prompt, str) or not 50 <= len(prompt) <= 4000
                    or len(name.strip()) < 3 or len(name) > 60):
                raise ValueError("Invalid custom-agent definition.")
            active.append({"name": name.strip(), "mission": mission.strip(), "prompt": prompt})
            applied.append(f"Hired {name}; it will join the next cycle.")
        elif kind == "retire":
            reason = action.get("reason")
            if not isinstance(reason, str) or len(reason.strip()) < 8:
                raise ValueError("Retirement requires an evidence-based reason.")
            if existing is not None:
                active.remove(existing)
                archived.append({"name": name, "mission": existing["mission"], "prompt": existing["prompt"],
                                 "kind": "custom", "reason": reason.strip(),
                                 "archived_utc": datetime.now(timezone.utc).isoformat()})
            elif name in CORE_AGENT_NAMES and name not in disabled:
                disabled.append(name)
                archived.append({"name": name, "kind": "core", "reason": reason.strip(),
                                 "archived_utc": datetime.now(timezone.utc).isoformat()})
            else:
                raise ValueError("Only an active non-manager agent can be retired.")
            applied.append(f"Retired {name}; it is disabled and its record was archived.")
        elif kind == "restore":
            if name in disabled:
                disabled.remove(name)
                applied.append(f"Restored core role {name}; it will run next cycle.")
            else:
                archived_item = next((item for item in reversed(archived)
                                      if item.get("name") == name and item.get("kind") == "custom"), None)
                if archived_item is None or len(active) >= MAX_CUSTOM_AGENTS:
                    raise ValueError("No restorable archived agent or custom-agent capacity is full.")
                active.append({"name": name, "mission": archived_item["mission"], "prompt": archived_item["prompt"]})
                archived.remove(archived_item)
                applied.append(f"Restored custom agent {name}; it will run next cycle.")
        else:
            raise ValueError("Unsupported HR action.")
    registry["archived_agents"] = archived[-50:]
    registry["role_overrides"] = overrides
    registry["disabled_agents"] = sorted(set(disabled))
    return decision, applied



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
    checkpoint = CheckpointStore(
        WORKSPACE / "CHECKPOINT.json",
        hashlib.sha256(request.encode("utf-8")).hexdigest(),
        repository_fingerprint(),
    )
    outputs = {
        key: value for key, value in checkpoint.outputs.items()
        if key != "__hr_output__"
    }
    resumed_stages = list(outputs)
    registry = read_team_registry()
    custom_agents = registry["custom_agents"]
    role_metrics = {}
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
        if name in outputs:
            continue
        if name in registry["disabled_agents"]:
            continue
        role = registry["role_overrides"].get(name, role)
        selected = prior_for_stage[name]
        prior = "\n\n".join(
            f"## {key}\n{outputs[key][:3500]}" for key in selected if key in outputs
        )
        runtime_evidence = ""
        if name == "Documentation Agent":
            runtime_evidence = (
                "\n\nLIVE EXECUTION EVIDENCE: The preceding role calls in this cycle "
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
        started = __import__("time").perf_counter()
        outputs[name] = call_with_retries(lambda: generator(role, prompt))
        checkpoint.record_stage(name, outputs[name])
        role_metrics[name] = {
            "duration_seconds": round(__import__("time").perf_counter() - started, 3),
            "output_characters": len(outputs[name]),
            "status": "success",
        }

    for custom in custom_agents:
        name = custom["name"]
        if name in outputs:
            continue
        started = __import__("time").perf_counter()
        custom_prompt = (
            context + "\n\nCUSTOM AGENT MISSION: " + custom["mission"]
            + "\n\nRun the specialist task using only the evidence and user request. "
            "Return concise findings with evidence, uncertainty, and actionable recommendations."
            + "\n\nPRIOR TEAM OUTPUTS:\n"
            + "\n\n".join(f"## {key}\n{value[:2500]}" for key, value in outputs.items())
        )
        outputs[name] = call_with_retries(lambda: generator(custom["prompt"], custom_prompt))
        checkpoint.record_stage(name, outputs[name])
        role_metrics[name] = {
            "duration_seconds": round(__import__("time").perf_counter() - started, 3),
            "output_characters": len(outputs[name]),
            "status": "success",
        }

    hr_prompt = (
        context + "\n\nTEAM ROSTER AND GOVERNANCE STATE:\n"
        + json.dumps({"custom_agents": custom_agents, "disabled_agents": registry["disabled_agents"],
                      "role_overrides": list(registry["role_overrides"].keys())}, ensure_ascii=False)[:8000]
        + "\n\nPERFORMANCE METRICS (latency and output size; not quality scores):\n"
        + json.dumps(role_metrics, ensure_ascii=False)
        + "\n\nALL ROLE OUTPUTS FOR EVIDENCE-BASED HR AUDIT:\n"
        + "\n\n".join(f"## {key}\n{value[:2500]}" for key, value in outputs.items())
        + "\n\nReturn the required strict JSON. Apply changes only to non-manager agents. "
        "Any manager concern must be a recommendation for human approval."
    )
    hr_started = __import__("time").perf_counter()
    hr_output = checkpoint.get_stage("__hr_output__")
    if hr_output is None:
        hr_output = call_with_retries(lambda: generator(HR, hr_prompt))
        checkpoint.record_stage("__hr_output__", hr_output)
    hr_duration = round(__import__("time").perf_counter() - hr_started, 3)
    try:
        hr_decision, hr_applied = apply_hr_decisions(hr_output, registry)
        hr_status = "valid"
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        hr_decision = {"summary": "HR response rejected; no staffing changes were applied.",
                       "manager_review": "none", "actions": []}
        hr_applied = [f"No changes applied: {type(exc).__name__}: {str(exc)[:250]}"]
        hr_status = "rejected"
    outputs["Agent Recruitment & Governance"] = (
        "### HR decision\n" + str(hr_decision.get("summary", "No summary supplied."))
        + "\n\n### Manager review\n" + str(hr_decision.get("manager_review", "none"))
        + "\n\n### Applied staffing actions\n"
        + ("\n".join("- " + item for item in hr_applied) if hr_applied else "- No staffing changes.")
        + "\n\n### HR response status\n" + hr_status
    )
    role_metrics["Agent Recruitment & Governance"] = {
        "duration_seconds": hr_duration,
        "status": "success" if hr_status == "valid" else "rejected",
        "output_characters": len(hr_output),
    }
    if hr_status == "valid" and (hr_applied or not TEAM_FILE.exists()):
        atomic_write_text(TEAM_FILE, json.dumps(registry, ensure_ascii=False, indent=2) + "\n")

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
        "role_metrics": role_metrics,
        "team_registry": "workspace/TEAM.json",
        "active_custom_agents": [item["name"] for item in registry["custom_agents"]],
        "disabled_core_agents": registry["disabled_agents"],
        "archived_custom_agents": [item.get("name") for item in registry["archived_agents"]],
        "hr_status": hr_status,
        "hr_manager_review": hr_decision.get("manager_review", "none"),
        "models_used": models_used,
        "models_attempted": models_attempted,
        "unit_test_methods": unit_test_methods,
        "github_run_id": __import__("os").getenv("GITHUB_RUN_ID"),
        "github_sha": __import__("os").getenv("GITHUB_SHA"),
        "next_step": "Review the report, approve decisions, update REQUEST.md, then run again.",
    }
    atomic_write_text(STATE_FILE, json.dumps(new_state, ensure_ascii=False, indent=2) + "\n")
    checkpoint.clear()
    return outputs



def write_failure_state(error):
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    state = read_state()
    checkpoint_path = WORKSPACE / "CHECKPOINT.json"
    completed = []
    try:
        checkpoint_data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if isinstance(checkpoint_data, dict) and isinstance(checkpoint_data.get("outputs"), dict):
            completed = [name for name in checkpoint_data["outputs"] if name != "__hr_output__"]
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    state.update({
        "status": "failed",
        "completed_agents": completed,
        "checkpoint_available": checkpoint_path.is_file(),
        "last_run_utc": datetime.now(timezone.utc).isoformat(),
        "last_error": str(error)[:1500],
        "next_step": "Rerun the workflow; matching request/source checkpoints will resume completed stages automatically.",
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
