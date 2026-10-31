"""Stage 2: a driver that runs a whole workflow itself through the Anthropic API.

It walks the same state machine as a Claude Code orchestrator, using the same MCP tools
in-process, but makes every model call itself. That buys two things the host can't give:
an exact token ledger (every response's usage is booked) and a fixed prompt prefix, so
the parts agents share are cache reads.

Prefix per agent, in render order, with a cache breakpoint after each stable part:
  tools (identical for every agent) + common.md + repo map  -> shared by the whole run
  role instructions                                         -> shared by every agent in a role
  packet + conversation (automatic caching moves along the tail)

Run:  agent-workflow-drive "<request>" --repo PATH --gate "pytest -q" [--gate ...]
      agent-workflow-drive --run RUN_ID      (resume, e.g. after replanning an escalation)

Security: bash commands come from the model and run unsandboxed as you, in the task's
worktree. Run the driver inside a container or VM for repos you don't fully trust.
"""

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import anthropic

from . import server as mcp
from .server import PROMPTS

MODELS = {"scout": "claude-haiku-5-5", "scribe": "claude-haiku-5-5", "architect": "claude-opus-5-5"}
DEFAULT_MODEL = "claude-sonnet-5-5"
HIGH_RISK_MODEL = "claude-opus-5-5"  # reviewer and verifier on risk "high" tasks
# Opus 5.5 defaults to medium effort; planning and high-risk review deserve more.
EFFORT = {"claude-opus-5-5": "high"}
# Claims, gates, replans and usage are the driver's job, as they are the orchestrator's.
DRIVER_ONLY = {"workflow_start", "usage_report", "gate_run", "task_claim", "role_brief",
               "task_release", "task_replan"}
EDITOR = "str_replace_based_edit_tool"
MAX_TURNS = 150
MAX_OUTPUT_CHARS = 30_000


class DriverError(Exception):
    """The run can't go on without a person: escalation, budget, or a dead agent."""


def log(message):
    print(message, file=sys.stderr, flush=True)


def tool_definitions():
    """The agent's tools, sorted so the cached prefix is byte-identical across agents."""
    workflow = [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in asyncio.run(mcp.server.list_tools()) if t.name not in DRIVER_ONLY
    ]
    return [{"type": "bash_20250124", "name": "bash"},
            {"type": "text_editor_20250728", "name": EDITOR},
            *sorted(workflow, key=lambda t: t["name"])]


def run_bash(cwd, command, timeout=600):
    # shortcut: every command gets a fresh shell, so cd and exports don't carry over;
    # switch to a persistent session if agents keep tripping on it.
    try:
        proc = subprocess.run(["bash", "-c", command], cwd=cwd, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"timed out after {timeout}s", True
    output = proc.stdout + proc.stderr
    if len(output) > MAX_OUTPUT_CHARS:
        output = "[... earlier output cut ...]\n" + output[-MAX_OUTPUT_CHARS:]
    return f"{output}\n(exit code {proc.returncode})", False


def edit_file(root, args):
    """The text editor tool, confined to root: model-supplied paths are untrusted."""
    root = Path(root).resolve()
    path = (root / args["path"]).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"{args['path']} is outside {root}")
    command = args["command"]
    if command == "view":
        if path.is_dir():
            return "\n".join(sorted(p.name + "/" * p.is_dir() for p in path.iterdir()))
        lines = path.read_text().splitlines()
        start, end = args.get("view_range") or (1, len(lines))
        end = len(lines) if end == -1 else end
        return "\n".join(f"{n}\t{line}" for n, line in enumerate(lines[start - 1:end], start))
    if command == "create":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["file_text"])
        return f"wrote {path}"
    if command == "str_replace":
        text = path.read_text()
        count = text.count(args["old_str"])
        if count != 1:
            raise ValueError(f"old_str occurs {count} times in {path}; it must occur exactly once")
        path.write_text(text.replace(args["old_str"], args.get("new_str", "")))
        return f"edited {path}"
    if command == "insert":
        text = args["insert_text"]
        lines = path.read_text().splitlines(keepends=True)
        lines.insert(args["insert_line"], text if text.endswith("\n") else text + "\n")
        path.write_text("".join(lines))
        return f"edited {path}"
    raise ValueError(f"unknown editor command {command}")


def call_tool(name, args, workdir):
    """Run one tool call; returns (text, is_error) so failures go back to the model."""
    try:
        if name == "bash":
            if args.get("restart"):
                return "shell restarted", False
            return run_bash(workdir, args["command"])
        if name == EDITOR:
            return edit_file(workdir, args), False
        if name in DRIVER_ONLY:
            raise ValueError(f"{name} is not available to agents")
        result = asyncio.run(mcp.server.call_tool(name, args))
        text = "\n".join(c.text for c in result.content if c.type == "text")
        return text, bool(result.is_error)
    except Exception as e:  # the model sees the error and can recover
        return f"{type(e).__name__}: {e}", True


def run_agent(client, run_id, role, task_id, context, workdir, model):
    """One subagent: a manual tool loop until the model stops calling tools."""
    store = mcp.store
    map_id = store.repo_map(run_id)
    repo_map = store.get_artifact(map_id)["content"] if map_id else "(no repo map yet)"
    system = [
        {"type": "text", "cache_control": {"type": "ephemeral"},
         "text": (PROMPTS / "common.md").read_text()
                 + f"\n# Repo map (artifact {map_id}; it is here already, don't fetch it)\n\n"
                 + repo_map},
        {"type": "text", "cache_control": {"type": "ephemeral"},
         "text": (PROMPTS / f"{role}.md").read_text()},
    ]
    messages = [{"role": "user", "content": f"You are the {role}. Your context:\n"
                                            + json.dumps(context, indent=1)}]
    extra = {}
    if model != "claude-haiku-5-5":  # Haiku has no server-side fallback
        extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    if model in EFFORT:
        extra["output_config"] = {"effort": EFFORT[model]}
    tools = tool_definitions()
    spent = cached = 0
    stop = "turn limit"
    for _ in range(MAX_TURNS):
        # Streaming because max_tokens is large. Custom tools leave eager input streaming
        # off: a truncated artifact_put would be stored without any error.
        with client.beta.messages.stream(model=model, max_tokens=64_000, system=system,
                                         tools=tools, messages=messages,
                                         cache_control={"type": "ephemeral"}, **extra) as stream:
            response = stream.get_final_message()
        usage = response.usage
        tokens = (usage.input_tokens + usage.output_tokens
                  + (usage.cache_creation_input_tokens or 0) + (usage.cache_read_input_tokens or 0))
        store.charge(run_id, role, tokens, task_id)
        spent += tokens
        cached += usage.cache_read_input_tokens or 0
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            stop = response.stop_reason
            break
        results = []
        for block in response.content:
            if block.type == "tool_use":
                text, is_error = call_tool(block.name, block.input, workdir)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": text, "is_error": is_error})
        messages.append({"role": "user", "content": results})
    log(f"  {role} {task_id or ''}: {spent} tokens, {cached * 100 // max(spent, 1)}% cache reads"
        + (f", stopped: {stop}" if stop != "end_turn" else ""))


def model_for(role, task=None):
    if role in ("reviewer", "verifier") and task and task["risk"] == "high":
        return HIGH_RISK_MODEL
    return MODELS.get(role, DEFAULT_MODEL)


def run_task_role(client, run_id, task, role):
    for attempt in (1, 2):
        try:
            claim = mcp.task_claim(run_id=run_id, task_id=task["id"], role=role)
        except Exception as e:
            raise DriverError(str(e)) from e
        packet = claim["packet"]
        run_agent(client, run_id, role, task["id"], packet,
                  packet["worktree"] or mcp.store.run_config(run_id)["repo"], model_for(role, task))
        if not any(t["claimed_by"] for t in mcp.store.list_tasks(run_id) if t["id"] == task["id"]):
            return
        mcp.store.release(run_id, task["id"])  # it ended without reporting
        log(f"  {role} {task['id']} did not report (attempt {attempt})")
    raise DriverError(f"{role} for {task['id']} twice ended without reporting")


def run_role(client, run_id, role):
    brief = mcp.role_brief(run_id=run_id, role=role)
    run_agent(client, run_id, role, None, brief["run"], mcp.store.run_config(run_id)["repo"],
              model_for(role))


def next_role(task):
    return {"planned": "test_writer" if task["tests_first"] else "coder",
            "tests_written": "coder", "fixing": "coder", "coded": "gate",
            "reviewing": "reviewer", "verifying": "verifier"}.get(task["state"])


def drive(client, run_id, approve=lambda tasks: True):
    """Run (or resume) a run to the end. Returns the scribe's report artifact id or None."""
    store = mcp.store
    if not store.repo_map(run_id):
        log("scout")
        run_role(client, run_id, "scout")
        if not store.repo_map(run_id):
            raise DriverError("the scout stored no repo map")
    if not store.list_tasks(run_id):
        log("architect")
        run_role(client, run_id, "architect")
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
        # shortcut: tasks run one step at a time, not in parallel; add a thread pool
        # (one store lock already makes the tools safe) if runs get slow.
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
                run_task_role(client, run_id, task, role)

    for role in ("integrator", "scribe"):
        log(role)
        run_role(client, run_id, role)
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

    try:
        run_id = args.run or mcp.workflow_start(
            request=args.request, repo=str(Path(args.repo).resolve()),
            gate_commands=args.gate, budget=args.budget)["run_id"]
        log(f"run {run_id}")
        report = drive(anthropic.Anthropic(), run_id, (lambda t: True) if args.yes else ask_approval)
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
