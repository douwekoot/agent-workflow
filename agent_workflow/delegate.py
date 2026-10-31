"""Jobs the driver hands to Copilot or Gemini instead of Claude, when asked with --use.

Each is opt-in, and each falls back to the Claude path when the other CLI fails, so a
missing login or a bad reply costs time but never the run. The other CLI only ever
produces text or edits files in its sandboxed working tree. This module books its usage,
commits its files and reports its results itself.
"""

import json
import re
import sys
from pathlib import Path

from . import external, gate
from . import server as mcp
from .server import PROMPTS

USE_CASES = {
    "scout": "build the repo map",
    "scribe": "write the final report",
    "plan_critic": "critique the architect's plan; the architect revises on findings",
    "second_opinion": "a second review of high-risk tasks, passed to the Claude reviewer",
    "test_writer": "write failing tests first",
    "coder_low": "implement low-risk tasks on their first attempt",
}
MAX_MAP_CHARS = 40_000
MIN_MAP_CHARS = 200
MAX_DIFF_CHARS = 80_000
MAX_INPUT_CHARS = 20_000
TEST_FILE = re.compile(r"(^|/)(tests?|__tests__)/|(^|/)test_[^/]*$|[_.](test|spec)\.[^/]+$")


def log(message):
    print(message, file=sys.stderr, flush=True)


def parse_uses(items):
    """["scout=gemini", "coder_low=copilot:gpt-5"] -> {"scout": "gemini", ...}"""
    uses = {}
    for item in items:
        name, _, spec = item.partition("=")
        if name not in USE_CASES or not spec:
            raise ValueError(f"--use wants NAME=PROVIDER[:MODEL] with NAME one of {', '.join(USE_CASES)}")
        external.split_spec(spec)
        uses[name] = spec
    return uses


def prompt(name, context):
    return ((PROMPTS / "external/common.md").read_text() + "\n" + (PROMPTS / f"external/{name}.md").read_text()
            + "\n# Context\n\n" + json.dumps(context, indent=1))


def _book(run_id, role, task_id, spec, result):
    """Book usage under the provider, outside the Claude budgets, and log the call."""
    provider = external.split_spec(spec)[0]
    if result.tokens:
        mcp.store.charge(run_id, role, result.tokens, task_id, provider)
    log(f"  {role} {task_id or ''}: {spec}, {result.tokens} tokens"
        + (f", {result.detail}" if result.detail else "")
        + ("" if result.ok else f", failed: {result.error}"))


def external_scout(run_id, spec):
    """True when the repo map was stored; False means run the Claude scout."""
    store, config = mcp.store, mcp.store.run_config(run_id)
    result = external.run(spec, prompt("scout", {"request": config["request"]}), config["repo"])
    _book(run_id, "scout", None, spec, result)
    if not result.ok or not MIN_MAP_CHARS <= len(result.text) <= MAX_MAP_CHARS:
        log(f"  repo map by {spec} unusable ({len(result.text)} chars), using the Claude scout")
        return False
    map_id = store.set_repo_map(run_id, result.text)
    store.record_run_result(run_id, "scout", "done", f"repo map by {spec}, {len(result.text)} chars", [map_id])
    return True


def external_scribe(run_id, spec):
    """The report artifact id, or None to run the Claude scribe."""
    store, config = mcp.store, mcp.store.run_config(run_id)
    context = {"request": config["request"], "tasks": store.list_tasks(run_id),
               "results": store.results(run_id), "ledger": store.ledger(run_id)}
    result = external.run(spec, prompt("scribe", context), config["repo"])
    _book(run_id, "scribe", None, spec, result)
    if not result.ok:
        return None
    report = store.put_artifact(run_id, "report", result.text)
    store.record_run_result(run_id, "scribe", "done", f"report by {spec}", [report])
    return report


def critique(run_id, spec):
    """Artifact id of the critique when it found problems, else None. Always leaves a
    'critic' result, so a resumed run doesn't critique the plan twice."""
    store, config = mcp.store, mcp.store.run_config(run_id)
    context = {"request": config["request"], "gate_commands": config["gate_commands"],
               "tasks": store.specs(run_id)}
    result = external.run(spec, prompt("plan_critic", context), config["repo"])
    _book(run_id, "critic", None, spec, result)
    findings = external.parse_findings(result.text) if result.ok else None
    if findings is None:
        store.record_run_result(run_id, "critic", "failed", f"no usable reply from {spec}")
        return None
    artifact = store.put_artifact(run_id, "plan_critique", "\n".join(json.dumps(f) for f in findings))
    store.record_run_result(run_id, "critic", "done", f"{len(findings)} findings by {spec}", [artifact])
    log(f"  plan critic: {len(findings)} findings")
    return artifact if findings else None


def second_opinion(run_id, task, spec):
    """Artifact id of another model's findings on the task's diff, or None."""
    store, repo = mcp.store, mcp.store.run_config(run_id)["repo"]
    worktree = mcp.home / "worktrees" / run_id / task["id"]
    diff = gate.diff_text(worktree, gate.current_branch(repo))
    if not diff.strip() or len(diff) > MAX_DIFF_CHARS:
        log(f"  second opinion skipped: diff is {len(diff)} chars (limit {MAX_DIFF_CHARS})")
        return None
    task_spec = next(s for s in store.specs(run_id) if s["id"] == task["id"])
    context = {"task": {k: task_spec[k] for k in ("goal", "owns", "interfaces", "acceptance")}, "diff": diff}
    result = external.run(spec, prompt("second_opinion", context), worktree)
    _book(run_id, "second_opinion", task["id"], spec, result)
    findings = external.parse_findings(result.text) if result.ok else None
    if not findings:
        return None
    return store.put_artifact(run_id, "second_opinion", "\n".join(json.dumps(f) for f in findings), task["id"])


def in_scope(path, owns, role):
    """Test files are always fair game; a test writer may touch nothing else."""
    if TEST_FILE.search(path):
        return True
    return role != "test_writer" and any(path == o or path.startswith(o.rstrip("/") + "/") for o in owns)


def _inline_inputs(inputs):
    """The previous step's artifacts as text, since the other CLI can't call artifact_get."""
    if not inputs:
        return None
    room, texts = MAX_INPUT_CHARS, {}
    for artifact_id in inputs["artifacts"]:
        texts[artifact_id] = mcp.store.get_artifact(artifact_id, length=room)["content"]
        room -= len(texts[artifact_id])
    return {"from": inputs["from"], "summary": inputs["summary"], "artifacts": texts}


def external_task(run_id, task, role, spec):
    """Let another CLI do a test writer's or coder's step in the task's worktree. True when
    its work was committed and reported; False means run the Claude agent, on a clean tree."""
    store = mcp.store
    try:
        packet = mcp.task_claim(run_id=run_id, task_id=task["id"], role=role)["packet"]
    except Exception as e:
        raise RuntimeError(str(e)) from e
    worktree = Path(packet["worktree"])
    context = {k: packet[k] for k in ("goal", "owns", "reads", "interfaces", "acceptance")}
    context["inputs"] = _inline_inputs(packet["inputs"])
    if packet["repo_map"]:
        context["repo_map"] = store.get_artifact(packet["repo_map"])["content"]
    result = external.run(spec, prompt(role, context), worktree, write=True)
    _book(run_id, role, task["id"], spec, result)
    if result.ok:
        stray = [p for p in gate.dirty_paths(worktree) if not in_scope(p, packet["owns"], role)]
        if stray:
            log(f"  {spec} touched files outside its scope, discarded: {', '.join(stray)}")
            gate.revert_paths(worktree, stray)
        message = f"{task['id']}: tests" if role == "test_writer" else f"{task['id']}: {packet['goal']}"
        if gate.commit_all(worktree, message[:200]):
            store.report(run_id, task["id"], role, "done", f"by {spec}: {result.text}"[:400])
            return True
        log(f"  {spec} changed nothing")
    gate.reset_worktree(worktree)
    store.release(run_id, task["id"])
    return False
