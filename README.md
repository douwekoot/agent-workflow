# agent-workflow

A local MCP server that coordinates several coding agents on one big request. Claude Code
is the orchestrator and spawns the subagents. The server holds the shared state: a task
state machine, a context packet per role, a token budget, and a sandboxed test gate. Each
task works in its own git worktree.

## Installation

Needs Python 3.11+, git, [Claude Code](https://claude.com/claude-code) and `bwrap`
(bubblewrap). Without `bwrap` the gate runs unsandboxed and says so.

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[test]"
.venv/bin/python -m pytest -q
claude mcp add --scope user agent-workflow -- "$PWD/.venv/bin/agent-workflow-mcp"
```

State lives in `$AGENT_WORKFLOW_HOME` (default `~/.local/share/agent-workflow`). Don't put
repos or that directory under `~/.claude`: the sandbox blocks writes there.

## Usage

From Claude Code, this session becomes the orchestrator:

```
/mcp__agent-workflow__orchestrate <your request>
```

Or run it headless. The driver runs each subagent as `claude -p` on your Claude
subscription:

```sh
.venv/bin/agent-workflow-drive "<request>" --repo /path/to/repo --gate "pytest -q"
.venv/bin/agent-workflow-drive --run <run_id>   # resume after a task_replan
```

The driver stops when a task escalates, the budget runs out, or the plan has more than 6
tasks (unless `--yes`). Tasks run one at a time. Agents run in a sandbox: writes only in
their worktree, no network, no access to `~/.ssh`, `~/.aws` or `~/.gnupg`.

Role prompts are in `agent_workflow/prompts/`. The tool list is in
`agent_workflow/server.py`.

## Other CLIs as second opinions

The driver can give some jobs to Copilot, Antigravity (`agy`) or Gemini with
`--use NAME=PROVIDER[:MODEL]`. If that CLI is missing or fails, Claude does the job.

```sh
npm install -g --prefix ~/.local @github/copilot @google/gemini-cli

.venv/bin/agent-workflow-drive "<request>" --repo /path --gate "pytest -q" \
  --use second_opinion=agy --use test_writer=copilot --use coder_low=copilot
```

Names: `scout`, `scribe`, `plan_critic`, `second_opinion` (high-risk diffs), `test_writer`,
`coder_low` (low-risk tasks, first attempt only).

- Copilot uses your `gh` login. Antigravity needs a one-time `agy` sign-in.
- Gemini needs `GEMINI_API_KEY` from Google AI Studio. Login with Google is rejected for
  consumer accounts.
- These CLIs have network access, so they run in a stricter sandbox. A prompt-injected agent
  could still read its own provider's token.
- Their tokens don't count against the Claude budget.

## License

This project is licensed under the [MIT License](LICENSE).

## Contact

Open an issue, or reach out to me at douwe@bitlotus.app
