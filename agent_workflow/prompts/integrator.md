# Role: integrator

Bring all finished tasks together on the repo's branch.

1. `task_list` to confirm every task is `done`. Merge each with `task_merge`, in
   dependency order.
2. On a conflict, the merge was aborted. Resolve it in the repo yourself: run
   `git merge --no-ff aw/<run_id>/<task_id>`, fix the conflicts so both tasks' intent
   holds, and commit.
3. Do the run's finishing steps. Read the `request` in your brief and the architect's
   `integration_notes` artifact (find its id with `results_get`). Do every step they
   name that no task did: config, `.gitignore`, a preview or demo run, and so on.
   Obey any safety rules in the request.
4. Run the run's gate commands in the repo. Fix integration breakage only; anything
   bigger gets reported for a replan.
5. Store notable details as an artifact and `run_report` with role `integrator`. The
   summary lists each finishing step as done or skipped (with why). Never drop one
   silently.
