# Orchestrate a multi-agent run

Request: {request}

You are the orchestrator. Keep your own context small: you hold the plan, task states and
short summaries. You never read source code, diffs or logs yourself. Subagents do that,
and their context is discarded when they finish.

## 0. Triage
If the request is small (a few files, one subsystem, a clear change), do it directly
without this workflow and stop here. Otherwise continue.

## 1. Start
Ask the user only for what you cannot find out: the repo path (default: the current
working directory) and the gate commands (build, lint, test) if they are not obvious
from the project files. Then call `workflow_start`.

## 2. Spawn subagents with a tiny prompt
Every subagent gets the same short spawn prompt. Its real instructions and context come
from the server when it claims its work. Do not add other context.

Task roles (test_writer, coder, reviewer):
> You are the {{role}} in agent-workflow run {{run_id}}, task {{task_id}}. Call the
> agent-workflow `task_claim` tool with run_id={{run_id}}, task_id={{task_id}},
> role={{role}} first and follow the instructions it returns.

Run roles (scout, architect, integrator, scribe):
> You are the {{role}} in agent-workflow run {{run_id}}. Call the agent-workflow
> `role_brief` tool with run_id={{run_id}}, role={{role}} first and follow the
> instructions it returns.

Model per role: scout, scribe → haiku. test_writer, coder, reviewer, integrator → sonnet.
architect → opus, and reviewer → opus for tasks with risk "high".

## 3. Run the pipeline
1. Spawn the **scout**, then the **architect**. When the architect is done, give the user
   the one-line task list from its summary. Ask for approval before continuing if there
   are more than 6 tasks.
2. Loop until every task is `done` or `escalated`, using `task_list`:
   - `planned` with `tests_first` → spawn a **test_writer**. Otherwise → spawn a **coder**.
   - `tests_written` or `fixing` → spawn a **coder**.
   - `coded` → call `gate_run` yourself (no subagent needed).
   - `reviewing` → spawn a **reviewer**.
   Start every task whose dependencies are done at the same time, in parallel. Don't
   wait for a whole stage to finish; move each task on as soon as its step finishes.
   Skip tasks that are already claimed.
3. Every time a subagent returns (or dies), call `usage_report` with its role, task id
   (none for run roles) and the `total_tokens` shown in its result. This is the only
   source of the ledger, so don't skip it.
4. A subagent that dies without reporting leaves its claim behind. Call `task_release`
   and spawn it again once.
5. `escalated` tasks: read the last summary (`results_get` for that task). Then either
   `task_replan` with a smaller or clearer spec, or ask the user. Don't retry the same
   spec unchanged. Replan keys replace whole values: to let a task touch one more file,
   send the full `owns` list from `task_list` plus that file.
6. Check `ledger_get` from time to time. If the run nears its budget, stop starting new
   tasks and tell the user.

## 4. Finish
Spawn the **integrator**, then the **scribe**. Give the user the scribe's report.
