# agent-workflow

A local MCP server that coordinates multi-role agent runs. It is Stage 1 of
`~/agent-workflow-design.md`. The host (for example Claude Code) is the orchestrator and
spawns subagents. This server holds the shared state and enforces the rules:

- **Per-task state machine.** Each task moves through
  `planned → [tests_written] → coded → reviewing → [verifying] → done`.
  Review findings go to a verifier, which drops false positives. When the gate fails or
  the verifier confirms a finding, the task goes to `fixing`. After `max_fix_loops`
  failures it moves to `escalated` and needs a replan or a decision.
- **Claims.** A role can only work on a task that is in the right state, and only one role
  at a time. The gate can't be skipped, and dependencies must be `done` first.
- **Context packets.** `task_claim` returns only what the role needs: the spec, the repo map
  id, and the artifacts from the previous step. The subagent never gets the conversation.
- **Artifacts.** Large output (diffs, logs, findings) is stored on disk and passed around by
  id. `artifact_get` reads it in slices.
- **Result caps.** A summary can be at most 400 characters. Detail goes in an artifact.
- **Token ledger.** There is a budget per task and per run. The orchestrator books each
  subagent's measured usage with `usage_report`; agents can't see their own usage. Packets carry a warning at 80%,
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

Suggested models: haiku for scout and scribe; sonnet for test writer, coder, reviewer,
verifier and integrator; opus for the architect and for reviewing high-risk tasks.

## Tools

| Tool | Used by |
|---|---|
| `workflow_start` | orchestrator: one run per big request (repo, gate commands, budget) |
| `repo_map_put` | scout |
| `task_create`, `task_replan` | architect / orchestrator |
| `task_list`, `ledger_get`, `results_get` | orchestrator, scribe |
| `task_claim`, `task_report`, `task_release` | test writer, coder, reviewer, verifier |
| `role_brief` | scout, architect, integrator, scribe (instructions + run context) |
| `gate_run` | orchestrator (runs the checks; no LLM) |
| `task_merge` | integrator |
| `run_report` | run-level roles (scout, architect, integrator, scribe) to finish |
| `usage_report` | orchestrator: books each subagent's measured token use in the ledger |
| `artifact_put`, `artifact_get` | everyone |

Prompt: `orchestrate(request)` is the orchestrator's playbook.

## Stage 2: the driver

`agent-workflow-drive` runs a whole workflow without an interactive session. It walks the
same state machine and calls the same tools in-process, and runs each subagent as a
headless Claude Code process (`claude -p`). It uses your Claude subscription, not the API:

```sh
.venv/bin/agent-workflow-drive "<request>" --repo /path/to/repo --gate "pytest -q"
.venv/bin/agent-workflow-drive --run <run_id>   # resume, e.g. after a task_replan
```

- **Exact ledger.** Each agent's reported usage is booked, so budgets hold. Cache reads
  count a tenth, as they cost: Claude Code rereads a ~20k-token base prompt every turn.
- **Prompt caching.** Common rules, the repo map and the role's instructions go into the
  appended system prompt. Agents in the same role then share Claude Code's prompt cache.
  Each agent logs its cache-read share.
- **Models.** Haiku for scout and scribe, Opus for the architect and for reviewing or
  verifying high-risk tasks, Sonnet for the rest.
- **Sandboxed agents.** Every agent's bash runs in Claude Code's sandbox (bubblewrap), with
  no unsandboxed fallback:
  - writes only inside its working directory (the task's worktree)
  - no network
  - `~/.ssh`, `~/.aws` and `~/.gnupg` can't be read
  
  File edits are limited to the working directory. Web tools and nested subagents are off.
  User settings, hooks and plugins are not loaded.
- It stops and tells you when a task escalates, the budget runs out, or a plan has more
  than 6 tasks (unless `--yes`). Fix it with the MCP tools, then resume with `--run`.
- Tasks run one step at a time, not in parallel.
- Don't put repos or `$AGENT_WORKFLOW_HOME` under `~/.claude`: the sandbox never allows
  writes there.

## Copilot, Antigravity and Gemini as second opinions and cheap workers

The driver can hand some jobs to GitHub Copilot, Antigravity (`agy`) or the Gemini CLI. All
are opt-in with `--use NAME=PROVIDER[:MODEL]`, repeatable, with provider `copilot`, `agy`
or `gemini`:

```sh
.venv/bin/agent-workflow-drive "<request>" --repo /path --gate "pytest -q" \
  --use second_opinion=agy --use plan_critic=agy --use test_writer=copilot \
  --use coder_low=copilot --use scout=agy --use scribe=agy
```

| `--use` name | What the other CLI does |
|---|---|
| `scout` | builds the repo map (read-only) |
| `scribe` | writes the final report (read-only) |
| `plan_critic` | critiques the architect's plan, once per run. On findings the architect revises with `task_replan` and `task_create`. |
| `second_opinion` | reviews the diff of every `risk: high` task. Its findings go to the Claude reviewer as candidates, and the verifier still checks what survives. |
| `test_writer` | writes the failing tests on `tests_first` tasks |
| `coder_low` | implements `risk: low` tasks, first attempt only. After a failed gate or review, Claude takes over. |

- **Fallback.** If the other CLI is missing, not signed in, times out or gives an unusable
  reply, the Claude agent does the job and the run goes on.
- **Scope guard.** Test writers may only change test files and coders only the files the
  task owns, plus tests. Anything else they leave behind is discarded. The CLIs never run
  git: the driver commits what they leave in the worktree.
- **Sandbox.** They need the network, so each call runs in bubblewrap with the rest hidden:
  `$HOME` is an empty tmpfs holding only the CLI install and the provider's own login
  directory, the environment is cleared, the session bus is hidden, and the working
  directory is read-only except for test writers and coders. A prompt-injected agent could
  still read that provider's own token.
- **Ledger.** Their tokens are listed under `external` and never count against the Claude
  budgets, since they come out of another quota. Copilot also logs its premium requests.
- **Setup.** `npm install -g --prefix ~/.local @github/copilot @google/gemini-cli`. Copilot
  uses your `gh` login. Antigravity: install `agy` and sign in once (the driver looks for
  `~/.gemini/antigravity-cli/antigravity-oauth-token`). It is slow to start, a few seconds
  per call. Write jobs run in its `auto` mode and read-only jobs in `plan` mode. Gemini
  needs `GEMINI_API_KEY`, a key from Google AI Studio: Login
  with Google no longer works, because Google rejects the Gemini CLI for consumer accounts
  and points them to Antigravity. An AI Studio key is billed or rate-limited apart from a
  Google AI subscription. The driver checks both before it starts.

## Gate sandbox

The gate runs tests, which is code the agents wrote. In both stages it runs inside
bubblewrap: no network, writes only inside the task's worktree plus a private `/tmp`,
secrets dirs hidden, and the session bus (`/run/user`) hidden. Without `bwrap` (e.g. on macOS) the gate runs unsandboxed and says so
in its result.
