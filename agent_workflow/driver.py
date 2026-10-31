"""Stage 2: a driver that runs a whole workflow on its own, with headless Claude Code.

It walks the same state machine as a Claude Code orchestrator, but each subagent is a
`claude -p` process, so it runs on your Claude subscription, not the API. That buys two
things the interactive host can't give:

- An exact ledger: each agent's reported usage is booked.
- A fixed prompt prefix: common rules + repo map + role instructions go in the appended
  system prompt, so agents of the same role reuse Claude Code's prompt cache.

Every agent runs in Claude Code's bash sandbox: writes only inside its working directory
(the task worktree), no network, no unsandboxed fallback, and secrets dirs unreadable.
File tools are confined to the working directory and web tools and subagents are off.

Run:  agent-workflow-drive "<request>" --repo PATH --gate "pytest -q" [--gate ...]
      agent-workflow-drive --run RUN_ID      (resume, e.g. after replanning an escalation)
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import server as mcp
from .server import PROMPTS

MODELS = {"scout": "haiku", "scribe": "haiku", "architect": "opus"}
DEFAULT_MODEL = "sonnet"
HIGH_RISK_MODEL = "opus"  # reviewer and verifier on risk "high" tasks
# Claims, gates, replans and usage are the driver's job, as they are the orchestrator's.
DRIVER_ONLY = {"workflow_start", "usage_report", "gate_run", "task_claim", "role_brief",
               "task_release", "task_replan"}
SECRETS = ["~/.ssh", "~/.aws", "~/.gnupg"]
SANDBOX = {"sandbox": {
    "enabled": True,
    "failIfUnavailable": True,          # never fall back to running bash unsandboxed
    "allowUnsandboxedCommands": False,  # and ignore the model's requests to escape it
    "autoAllowBashIfSandboxed": True,
    "filesystem": {"denyRead": SECRETS},
}}
ALLOWED_TOOLS = ["Bash", "Read", "Glob", "Grep", "Edit(/**)", "Write(/**)", "mcp__agent-workflow"]
DENIED_TOOLS = ["Agent", "WebFetch", "WebSearch",
                *(f"Read({d}/**)" for d in SECRETS),
                *(f"mcp__agent-workflow__{name}" for name in sorted(DRIVER_ONLY))]
AGENT_TIMEOUT = 3600
CACHE_READ_DIVISOR = 10


class DriverError(Exception):
    """The run can't go on without a person: escalation, budget, or a dead agent."""


def log(message):
    print(message, file=sys.stderr, flush=True)


def claude_command(prompt, system, model):
    mcp_config = {"mcpServers": {"agent-workflow": {
        "command": sys.executable, "args": ["-m", "agent_workflow.server"],
        "env": {"AGENT_WORKFLOW_HOME": str(mcp.home)},
    }}}
    return [os.environ.get("AGENT_WORKFLOW_CLAUDE", "claude"), "-p", prompt,
            "--model", model, "--output-format", "json",
            # dontAsk refuses anything not allowed below instead of waiting for a prompt.
            "--permission-mode", "dontAsk", "--no-session-persistence",
            # No user settings, hooks or plugins: every agent gets the same prefix.
            "--setting-sources", "",
            "--strict-mcp-config", "--mcp-config", json.dumps(mcp_config),
            "--settings", json.dumps(SANDBOX),
            "--append-system-prompt", system,
            "--allowedTools", *ALLOWED_TOOLS, "--disallowedTools", *DENIED_TOOLS]


def run_agent(run_id, role, task_id, context, workdir, model):
    """One subagent: a sandboxed `claude -p` run; its usage is booked in the ledger."""
    store = mcp.store
    map_id = store.repo_map(run_id)
    repo_map = store.get_artifact(map_id)["content"] if map_id else "(no repo map yet)"
    system = ((PROMPTS / "common.md").read_text()
              + f"\n# Repo map (artifact {map_id}; it is here already, don't fetch it)\n\n{repo_map}\n\n"
              + (PROMPTS / f"{role}.md").read_text())
    prompt = f"You are the {role}. Your context:\n" + json.dumps(context, indent=1)
    try:
        proc = subprocess.run(claude_command(prompt, system, model), cwd=workdir,
                              capture_output=True, text=True, timeout=AGENT_TIMEOUT)
    except subprocess.TimeoutExpired:
        log(f"  {role} {task_id or ''}: timed out after {AGENT_TIMEOUT}s, usage unknown")
        return
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise DriverError(f"claude failed for {role} (exit {proc.returncode}): "
                          f"{(proc.stderr or proc.stdout)[-500:]}") from None
    usage = result.get("usage") or {}
    cached = usage.get("cache_read_input_tokens") or 0
    fresh = sum(usage.get(k) or 0 for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens"))
    # Claude Code rereads its ~20k-token base prompt from cache every turn. Cache reads cost
    # a tenth of fresh input, so they count a tenth; at full weight they swamp the budgets.
    store.charge(run_id, role, fresh + cached // CACHE_READ_DIVISOR, task_id)
    log(f"  {role} {task_id or ''}: {fresh} fresh + {cached} cache-read tokens"
        + (f", error: {result.get('subtype')}" if result.get("is_error") else ""))


def model_for(role, task=None):
    if role in ("reviewer", "verifier") and task and task["risk"] == "high":
        return HIGH_RISK_MODEL
    return MODELS.get(role, DEFAULT_MODEL)


def run_task_role(run_id, task, role):
    for attempt in (1, 2):
        try:
            claim = mcp.task_claim(run_id=run_id, task_id=task["id"], role=role)
        except Exception as e:
            raise DriverError(str(e)) from e
        packet = claim["packet"]
        run_agent(run_id, role, task["id"], packet,
                  packet["worktree"] or mcp.store.run_config(run_id)["repo"], model_for(role, task))
        if not any(t["claimed_by"] for t in mcp.store.list_tasks(run_id) if t["id"] == task["id"]):
            return
        mcp.store.release(run_id, task["id"])  # it ended without reporting
        log(f"  {role} {task['id']} did not report (attempt {attempt})")
    raise DriverError(f"{role} for {task['id']} twice ended without reporting")


def run_role(run_id, role):
    brief = mcp.role_brief(run_id=run_id, role=role)
    run_agent(run_id, role, None, brief["run"], mcp.store.run_config(run_id)["repo"], model_for(role))


def next_role(task):
    return {"planned": "test_writer" if task["tests_first"] else "coder",
            "tests_written": "coder", "fixing": "coder", "coded": "gate",
            "reviewing": "reviewer", "verifying": "verifier"}.get(task["state"])


def drive(run_id, approve=lambda tasks: True):
    """Run (or resume) a run to the end. Returns the scribe's report artifact id or None."""
    store = mcp.store
    if not store.repo_map(run_id):
        log("scout")
        run_role(run_id, "scout")
        if not store.repo_map(run_id):
            raise DriverError("the scout stored no repo map")
    if not store.list_tasks(run_id):
        log("architect")
        run_role(run_id, "architect")
        tasks = store.list_tasks(run_id)
        if not tasks:
            raise DriverError("the architect created no tasks")
        if len(tasks) > 6 and not approve(tasks):
            raise DriverError("plan not approved; replan with task_replan, then resume with --run")

    while True:
        tasks = store.list_tasks(run_id)
        state = {t["id"]: t["state"] for t in tasks}
        if all(s == "done" for s in state.values()):
            break
        # shortcut: tasks run one step at a time, not in parallel; run agents in threads
        # (the tools are already safe for that) if runs get slow.
        ready = [t for t in tasks if next_role(t) and not t["claimed_by"]
                 and all(state[d] == "done" for d in t["depends_on"])]
        if not ready:
            stuck = {i: s for i, s in state.items() if s != "done"}
            raise DriverError(f"no task can move: {stuck}. Replan escalated tasks "
                              f"(results_get shows why), then resume with --run {run_id}")
        for task in ready:
            role = next_role(task)
            log(f"{task['id']} -> {role}")
            if role == "gate":
                outcome = mcp.gate_run(run_id=run_id, task_id=task["id"])
                log(f"  gate {'passed' if outcome['passed'] else 'failed'}")
            else:
                run_task_role(run_id, task, role)

    for role in ("integrator", "scribe"):
        log(role)
        run_role(run_id, role)
    reports = [a for r in store.results(run_id) if r["role"] == "scribe" for a in r["artifacts"]]
    return reports[-1] if reports else None


def ask_approval(tasks):
    for t in tasks:
        print(f"  {t['id']}: {t['goal']}", file=sys.stderr)
    return input(f"{len(tasks)} tasks. Continue? [y/N] ").strip().lower() == "y"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agent-workflow-drive", description=__doc__.split("\n")[0])
    parser.add_argument("request", nargs="?", help="what to build (omit with --run)")
    parser.add_argument("--repo", default=".", help="git repository (default: current directory)")
    parser.add_argument("--gate", action="append", default=[], help="gate command; repeat in order")
    parser.add_argument("--budget", type=int, default=500_000)
    parser.add_argument("--run", help="resume this run id instead of starting one")
    parser.add_argument("--yes", action="store_true", help="don't ask to approve plans over 6 tasks")
    args = parser.parse_args(argv)
    if bool(args.request) == bool(args.run):
        parser.error("give a request or --run, not both")
    if not shutil.which(os.environ.get("AGENT_WORKFLOW_CLAUDE", "claude")):
        parser.error("the claude CLI is not on PATH")

    try:
        run_id = args.run or mcp.workflow_start(
            request=args.request, repo=str(Path(args.repo).resolve()),
            gate_commands=args.gate, budget=args.budget)["run_id"]
        log(f"run {run_id}")
        report = drive(run_id, (lambda t: True) if args.yes else ask_approval)
    except (DriverError, mcp.ToolError) as e:
        log(f"stopped: {e}")
        return 1
    ledger = mcp.store.ledger(run_id)
    log(f"done: {ledger['spent']} of {ledger['budget']} tokens")
    if report:
        print(mcp.store.get_artifact(report)["content"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
