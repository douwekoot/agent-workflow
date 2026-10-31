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

Optionally (--use NAME=PROVIDER), some jobs go to GitHub Copilot or Gemini instead: see
delegate.USE_CASES. They run in their own sandbox, and fall back to Claude if they fail.

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

from . import delegate, external
from . import server as mcp
from .delegate import log
from .server import PROMPTS

MODELS = {"scout": "haiku", "scribe": "haiku", "architect": "opus"}
DEFAULT_MODEL = "sonnet"
HIGH_RISK_MODEL = "opus"  # reviewer and verifier on risk "high" tasks
# Claims, gates, replans and usage are the driver's job, as they are the orchestrator's.
DRIVER_ONLY = {"workflow_start", "usage_report", "gate_run", "task_claim", "role_brief",
               "task_release", "task_replan"}
# Other vendors' logins too: a Claude agent has no business with them.
SECRETS = ["~/.ssh", "~/.aws", "~/.gnupg", "~/.gemini", "~/.copilot", "~/.config/gh"]
SANDBOX = {"sandbox": {
    "enabled": True,
    "failIfUnavailable": True,          # never fall back to running bash unsandboxed
    "allowUnsandboxedCommands": False,  # and ignore the model's requests to escape it
    "autoAllowBashIfSandboxed": True,
    "filesystem": {"denyRead": SECRETS},
}}
ALLOWED_TOOLS = ["Bash", "Read", "Glob", "Grep", "Edit(/**)", "Write(/**)", "mcp__agent-workflow"]


def denied_tools(role):
    # The architect revises plans, so it alone may replan.
    driver_only = DRIVER_ONLY - {"task_replan"} if role == "architect" else DRIVER_ONLY
    return ["Agent", "WebFetch", "WebSearch", *(f"Read({d}/**)" for d in SECRETS),
            *(f"mcp__agent-workflow__{name}" for name in sorted(driver_only))]

AGENT_TIMEOUT = 3600
CACHE_READ_DIVISOR = 10


class DriverError(Exception):
    """The run can't go on without a person: escalation, budget, or a dead agent."""


def claude_command(prompt, system, model, role):
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
            "--allowedTools", *ALLOWED_TOOLS, "--disallowedTools", *denied_tools(role)]


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
        proc = subprocess.run(claude_command(prompt, system, model, role), cwd=workdir,
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


def external_choice(task, role, uses):
    """The provider spec that should do this step, or None for Claude. A cheap coder gets
    one attempt only: after a failed gate or review, Claude takes over."""
    if role == "test_writer":
        return uses.get("test_writer")
    if role == "coder" and task["risk"] == "low" and task["fix_loops"] == 0:
        return uses.get("coder_low")
    return None


def run_task_role(run_id, task, role, uses=None):
    uses = uses or {}
    spec = external_choice(task, role, uses)
    if spec:
        try:
            if delegate.external_task(run_id, task, role, spec):
                return
        except RuntimeError as e:
            raise DriverError(str(e)) from e
        log(f"  {role} {task['id']}: {spec} failed, using Claude")
    extra = {}
    if role == "reviewer" and task["risk"] == "high" and "second_opinion" in uses:
        opinion = delegate.second_opinion(run_id, task, uses["second_opinion"])
        if opinion:
            extra["second_opinion"] = opinion
    for attempt in (1, 2):
        try:
            claim = mcp.task_claim(run_id=run_id, task_id=task["id"], role=role)
        except Exception as e:
            raise DriverError(str(e)) from e
        packet = {**claim["packet"], **extra}
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


def revise_plan(run_id, critique):
    """One more architect pass, with the critic's findings and the current plan."""
    brief = mcp.role_brief(run_id=run_id, role="architect")
    context = {**brief["run"], "revise": True, "critique": critique, "tasks": mcp.store.specs(run_id)}
    run_agent(run_id, "architect", None, context, mcp.store.run_config(run_id)["repo"],
              model_for("architect"))


def drive(run_id, approve=lambda tasks: True, uses=None):
    """Run (or resume) a run to the end. Returns the scribe's report artifact id or None."""
    store, uses = mcp.store, uses or {}
    if not store.repo_map(run_id):
        log("scout")
        if not ("scout" in uses and delegate.external_scout(run_id, uses["scout"])):
            run_role(run_id, "scout")
        if not store.repo_map(run_id):
            raise DriverError("the scout stored no repo map")
    planned = not store.list_tasks(run_id)
    if planned:
        log("architect")
        run_role(run_id, "architect")
        if not store.list_tasks(run_id):
            raise DriverError("the architect created no tasks")
    if "plan_critic" in uses and not any(r["role"] == "critic" for r in store.results(run_id)):
        log("plan critic")
        findings = delegate.critique(run_id, uses["plan_critic"])
        if findings:
            log("architect (revising)")
            revise_plan(run_id, findings)
    tasks = store.list_tasks(run_id)
    if planned and len(tasks) > 6 and not approve(tasks):
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
                run_task_role(run_id, task, role, uses)

    log("integrator")
    run_role(run_id, "integrator")
    log("scribe")
    if not ("scribe" in uses and delegate.external_scribe(run_id, uses["scribe"])):
        run_role(run_id, "scribe")
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
    parser.add_argument("--use", action="append", default=[], metavar="NAME=PROVIDER[:MODEL]",
                        help="hand a job to copilot or gemini instead of Claude; NAME is one of: "
                        + "; ".join(f"{n} ({d})" for n, d in delegate.USE_CASES.items()))
    args = parser.parse_args(argv)
    if bool(args.request) == bool(args.run):
        parser.error("give a request or --run, not both")
    if not shutil.which(os.environ.get("AGENT_WORKFLOW_CLAUDE", "claude")):
        parser.error("the claude CLI is not on PATH")
    try:
        uses = delegate.parse_uses(args.use)
    except ValueError as e:
        parser.error(str(e))
    for spec in sorted(set(uses.values())):
        problem = external.check(spec)
        if problem:
            parser.error(f"--use {spec}: {problem}")

    try:
        run_id = args.run or mcp.workflow_start(
            request=args.request, repo=str(Path(args.repo).resolve()),
            gate_commands=args.gate, budget=args.budget)["run_id"]
        log(f"run {run_id}")
        report = drive(run_id, (lambda t: True) if args.yes else ask_approval, uses)
    except (DriverError, mcp.ToolError) as e:
        log(f"stopped: {e}")
        return 1
    ledger = mcp.store.ledger(run_id)
    log(f"done: {ledger['spent']} of {ledger['budget']} Claude tokens"
        + "".join(f", {t} {p} tokens" for p, t in ledger["external"].items()))
    if report:
        print(mcp.store.get_artifact(report)["content"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
