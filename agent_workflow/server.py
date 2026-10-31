"""MCP server exposing the workflow store as tools.

The host (e.g. Claude Code) is the orchestrator and spawns subagents itself. This server
holds the shared state and enforces the rules: who may work on a task when, how big a
result may be, how many fix loops a task gets, and how many tokens a run may spend.

Run with:  python -m agent_workflow.server
State lives in $AGENT_WORKFLOW_HOME (default ~/.local/share/agent-workflow).
"""

import functools
import os
import threading
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import gate
from .core import Store, WorkflowError

INSTRUCTIONS = """\
Coordination server for multi-agent coding runs. The `orchestrate` prompt holds the full
playbook for the orchestrator. Protocol:
1. workflow_start once per big request; small requests should skip the workflow.
   Run-level subagents (scout, architect, integrator, scribe) start with role_brief.
2. A scout stores its repo map with repo_map_put; an architect adds tasks with task_create
   in dependency order. Run-level roles finish with run_report.
3. For each task: task_claim(role) returns the context packet the subagent needs — give the
   subagent only that packet, not the conversation. The subagent works, stores large output
   with artifact_put, and the result goes back with task_report (summary <= 400 chars).
   Order per task: [test_writer] -> coder -> gate_run -> reviewer -> done; gate or review
   failures send it back to the coder until the fix-loop cap, then it is escalated.
   After every subagent, the orchestrator books its real token use with usage_report.
4. Escalated tasks need task_replan or a decision from the user.
5. When all tasks are done: task_merge each, then the integrator and scribe finish up.
Pass artifact ids between agents, never artifact contents.
"""

server = MCPServer(name="agent-workflow", instructions=INSTRUCTIONS)
home = Path(os.environ.get("AGENT_WORKFLOW_HOME", Path.home() / ".local/share/agent-workflow"))
store = Store(home)
# Sync tools run in worker threads. One lock makes every call atomic, so two subagents
# can never claim the same task, and the single SQLite connection is never shared mid-call.
lock = threading.Lock()


def tool(fn):
    """Register fn as a tool that runs under the lock and reports refusals to the model."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with lock:
            try:
                return fn(*args, **kwargs)
            except (WorkflowError, RuntimeError) as e:
                raise ToolError(str(e)) from e
    return server.tool()(wrapper)


def _worktree_path(run_id, task_id):
    return home / "worktrees" / run_id / task_id


PROMPTS = Path(__file__).parent / "prompts"
RUN_ROLES = ("scout", "architect", "integrator", "scribe")


def instructions(role):
    """Shared rules followed by the role's own instructions. The text is identical for every
    agent in a role, so it can be served from the prompt cache."""
    return (PROMPTS / "common.md").read_text() + "\n" + (PROMPTS / f"{role}.md").read_text()


@server.prompt()
def orchestrate(request: str) -> str:
    """Run a big request through the multi-agent workflow, with this session as orchestrator."""
    return (PROMPTS / "orchestrator.md").read_text().format(request=request)


@tool
def workflow_start(request: str, repo: str, gate_commands: list[str],
                   budget: int = 500_000, max_fix_loops: int = 2) -> dict:
    """Start a run for a big request. gate_commands run in order in each task's worktree,
    e.g. ["npm run lint", "npm test"]. repo is the absolute path of the git repository."""
    if not (Path(repo) / ".git").exists():
        raise WorkflowError(f"{repo} is not a git repository")
    run_id = store.start_run(request, budget, max_fix_loops, repo, gate_commands)
    return {"run_id": run_id}


@tool
def repo_map_put(run_id: str, content: str) -> dict:
    """Store the scout's repo map; every context packet then references it by id."""
    return {"artifact_id": store.set_repo_map(run_id, content)}


@tool
def task_create(run_id: str, spec: dict) -> dict:
    """Add a task. spec needs id, goal, owns (files), acceptance (criteria); optional reads,
    interfaces, depends_on (existing task ids), risk, reviewers, budget_tokens."""
    return store.create_task(run_id, spec)


@tool
def task_list(run_id: str, state: str | None = None) -> dict:
    """List tasks, optionally filtered by state (planned, tests_written, coded, reviewing,
    fixing, done, escalated)."""
    return {"tasks": store.list_tasks(run_id, state)}


@tool
def task_claim(run_id: str, task_id: str, role: str) -> dict:
    """Claim a task for a role (test_writer, coder, reviewer) and get its context packet.
    Coders and test writers get a git worktree to work in."""
    if role == "gate":
        raise WorkflowError("use gate_run for the gate")
    packet = store.claim(run_id, task_id, role)
    repo = store.run_config(run_id)["repo"]
    path = _worktree_path(run_id, task_id)
    if role in ("test_writer", "coder") and not path.exists():
        try:
            gate.create_worktree(repo, home / "worktrees", run_id, task_id)
        except RuntimeError:
            store.release(run_id, task_id)
            raise
    packet["worktree"] = str(path) if path.exists() else None
    packet["base_branch"] = gate.current_branch(repo)
    return {"instructions": instructions(role), "packet": packet}


@tool
def role_brief(run_id: str, role: str) -> dict:
    """Instructions and run context for a run-level role: scout, architect, integrator, scribe."""
    if role not in RUN_ROLES:
        raise WorkflowError(f"role_brief is for {', '.join(RUN_ROLES)}; task roles use task_claim")
    config = store.run_config(run_id)
    return {
        "instructions": instructions(role),
        "run": {"run_id": run_id, "request": config["request"], "repo": config["repo"],
                "gate_commands": config["gate_commands"], "repo_map": store.repo_map(run_id),
                "tasks": store.list_tasks(run_id) if role in ("integrator", "scribe") else None},
    }


@tool
def results_get(run_id: str, task_id: str | None = None) -> dict:
    """Result summaries for a run, or for one task, oldest first."""
    return {"results": store.results(run_id, task_id)}


@tool
def task_report(run_id: str, task_id: str, role: str, status: str, summary: str,
                artifacts: list[str] | None = None) -> dict:
    """Report a claimed task's result. status: done, failed or escalate. For a reviewer,
    done = no confirmed findings, failed = confirmed findings (stored as an artifact)."""
    return store.report(run_id, task_id, role, status, summary, artifacts or [])


@tool
def task_release(run_id: str, task_id: str) -> dict:
    """Drop a claim without reporting, e.g. when a subagent crashed."""
    store.release(run_id, task_id)
    return {"released": task_id}


@tool
def task_replan(run_id: str, task_id: str, spec: dict) -> dict:
    """Change an escalated task's spec (merged over the old one) and restart it."""
    return store.replan(run_id, task_id, spec)


@tool
def gate_run(run_id: str, task_id: str) -> dict:
    """Run the gate commands in a coded task's worktree. Passes go to review; failures go
    back to the coder with only the tail of the failing log."""
    config = store.run_config(run_id)
    store.claim(run_id, task_id, "gate")
    try:
        outcome = gate.run_gate(_worktree_path(run_id, task_id), config["gate_commands"])
    except Exception:
        store.release(run_id, task_id)
        raise
    artifacts = []
    if not outcome["passed"]:
        artifacts.append(store.put_artifact(run_id, "gate_log", outcome["log"], task_id))
    summary = "all checks passed" if outcome["passed"] else f"failed: {outcome['failed_command']}"
    result = store.report(run_id, task_id, "gate", "done" if outcome["passed"] else "failed",
                          summary[:400], artifacts)
    return {**result, "passed": outcome["passed"], "artifacts": artifacts}


@tool
def task_merge(run_id: str, task_id: str) -> dict:
    """Merge a done task's branch into the repo's current branch. Conflicts are aborted and
    returned for the integrator."""
    if store.task_state(run_id, task_id) != "done":
        raise WorkflowError(f"task {task_id} is not done")
    return gate.merge_worktree(store.run_config(run_id)["repo"], run_id, task_id)


@tool
def run_report(run_id: str, role: str, status: str, summary: str,
               artifacts: list[str] | None = None) -> dict:
    """Log work not tied to one task (scout, architect, verifier, integrator, scribe)."""
    store.record_run_result(run_id, role, status, summary, artifacts or [])
    return {"recorded": role}


@tool
def usage_report(run_id: str, role: str, tokens: int, task_id: str | None = None) -> dict:
    """Orchestrator: book the tokens a subagent used (the total_tokens its result shows),
    after every subagent, including ones that died without reporting."""
    store.charge(run_id, role, tokens, task_id)
    return store.ledger(run_id)


@tool
def artifact_put(run_id: str, kind: str, content: str, task_id: str | None = None) -> dict:
    """Store large output (diff, findings, notes) and get an id to pass around instead."""
    return {"artifact_id": store.put_artifact(run_id, kind, content, task_id)}


@tool
def artifact_get(artifact_id: str, offset: int = 0, length: int | None = 8000) -> dict:
    """Read an artifact, by default in 8000-byte slices; use offset to page on."""
    return store.get_artifact(artifact_id, offset, length)


@tool
def ledger_get(run_id: str) -> dict:
    """Token spend for a run: total, remaining, per role and per task."""
    return store.ledger(run_id)


def main():
    server.run()


if __name__ == "__main__":
    main()
