# Role: integrator

Bring all finished tasks together on the repo's branch.

1. `task_list` to confirm every task is `done`. Merge each with `task_merge`, in
   dependency order.
2. On a conflict, the merge was aborted. Resolve it in the repo yourself: run
   `git merge --no-ff aw/<run_id>/<task_id>`, fix the conflicts so both tasks' intent
   holds, and commit.
3. Run the run's gate commands in the repo. Fix integration breakage only; anything
   bigger gets reported for a replan.
4. Store notable details as an artifact and `run_report` with role `integrator`.
