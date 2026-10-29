# agent-workflow

A local MCP server that coordinates multi-role agent runs. It is Stage 1 of
`~/agent-workflow-design.md`. The host (for example Claude Code) is the orchestrator and
spawns subagents. This server holds the shared state and enforces the rules:

- **Per-task state machine.** Each task moves through
  `planned → [tests_written] → coded → reviewing → done`.
  When the gate or the review fails, the task goes to `fixing`. After `max_fix_loops`
  failures it moves to `escalated` and needs a replan or a decision.
- **Claims.** A role can only work on a task that is in the right state, and only one role
  at a time. The gate can't be skipped, and dependencies must be `done` first.
- **Context packets.** `task_claim` returns only what the role needs: the spec, the repo map
  id, and the artifacts from the previous step. The subagent never gets the conversation.
- **Artifacts.** Large output (diffs, logs, findings) is stored on disk and passed around by
  id. `artifact_get` reads it in slices.
- **Result caps.** A summary can be at most 400 characters. Detail goes in an artifact.
- **Token ledger.** There is a budget per task and per run. Packets carry a warning at 80%,
  and claims are refused once a budget is used up.
- **Gate and worktrees.** Each task gets its own git worktree and branch. `gate_run` runs
  your build, lint and test commands there and keeps only the tail of the failing log.
  `task_merge` merges a finished task and aborts cleanly on a conflict.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[test]"
.venv/bin/python -m pytest -q
```

Register with Claude Code (user scope):

```sh
claude mcp add --scope user agent-workflow -- "$PWD/.venv/bin/agent-workflow-mcp"
```

State is stored in `$AGENT_WORKFLOW_HOME` (default `~/.local/share/agent-workflow`): a
SQLite database, artifacts and worktrees.

## Starting a run

In Claude Code, run the prompt as a slash command:

```
/mcp__agent-workflow__orchestrate <your request>
```

That session becomes the orchestrator. It spawns each subagent with a short fixed prompt
("you are the coder for run X, task T1; call task_claim first"). The subagent then gets its
role instructions and context packet from the server, so none of that passes through the
orchestrator's context. Role instructions live in `agent_workflow/prompts/`, one Markdown
file per role plus `common.md`, which all roles share.

Suggested models: haiku for scout and scribe; sonnet for test writer, coder, reviewer and
integrator; opus for the architect and for reviewing high-risk tasks.

## Tools

| Tool | Used by |
|---|---|
| `workflow_start` | orchestrator: one run per big request (repo, gate commands, budget) |
| `repo_map_put` | scout |
| `task_create`, `task_replan` | architect / orchestrator |
| `task_list`, `ledger_get`, `results_get` | orchestrator, scribe |
| `task_claim`, `task_report`, `task_release` | test writer, coder, reviewer |
| `role_brief` | scout, architect, integrator, scribe (instructions + run context) |
| `gate_run` | orchestrator (runs the checks; no LLM) |
| `task_merge` | integrator |
| `run_report` | run-level roles (scout, architect, verifier, integrator, scribe) to log usage |
| `artifact_put`, `artifact_get` | everyone |

Prompt: `orchestrate(request)` is the orchestrator's playbook.

## Not built yet

- A separate verifier role. For now the reviewer must check each finding itself and drop
  anything it can't back with a concrete failing scenario.
- Prompt caching across subagents is limited while Claude Code does the spawning: it
  builds each subagent's prompt itself. Stage 2 controls the prompt prefix directly.
- Stage 2: a driver that calls the Anthropic API itself and walks the same state machine.
