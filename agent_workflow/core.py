"""Shared state for the multi-agent workflow: runs, tasks, artifacts and the token ledger.

The store is the only thing that knows where a task is in its lifecycle. Agents claim a
task for a role, receive a small context packet, and report a small result. Everything
large (diffs, logs, findings, the repo map) lives in artifacts and is passed by id.
"""

import json
import sqlite3
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    request TEXT NOT NULL,
    budget INTEGER NOT NULL,
    max_fix_loops INTEGER NOT NULL,
    repo TEXT,
    gate_commands TEXT NOT NULL DEFAULT '[]',
    repo_map TEXT,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    run_id TEXT NOT NULL REFERENCES runs(id),
    id TEXT NOT NULL,
    spec TEXT NOT NULL,
    state TEXT NOT NULL,
    claimed_by TEXT,
    fix_loops INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, id)
);
CREATE TABLE IF NOT EXISTS results (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    task_id TEXT,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL,
    artifacts TEXT NOT NULL,
    tokens INTEGER NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    task_id TEXT,
    role TEXT NOT NULL,
    tokens INTEGER NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT,
    kind TEXT NOT NULL,
    size INTEGER NOT NULL,
    created REAL NOT NULL
);
"""

# Which roles may claim a task in each state. The gate is not an LLM, but it claims and
# reports like any other role so the state machine cannot be skipped.
CLAIMABLE = {
    "planned": {"test_writer", "coder"},
    "tests_written": {"coder"},
    "coded": {"gate"},
    "reviewing": {"reviewer"},
    "fixing": {"coder"},
}

# (role, status) -> next state. Anything not listed, and any "escalate", goes to "escalated".
NEXT_STATE = {
    ("test_writer", "done"): "tests_written",
    ("coder", "done"): "coded",
    ("gate", "done"): "reviewing",
    ("gate", "failed"): "fixing",
    ("reviewer", "done"): "done",
    ("reviewer", "failed"): "fixing",
}

TASK_ROLES = set().union(*CLAIMABLE.values())
ROLES = {"orchestrator", "scout", "architect", "test_writer", "coder", "gate",
         "reviewer", "verifier", "integrator", "scribe"}
STATUSES = {"done", "failed", "escalate"}
REQUIRED_SPEC_KEYS = ("id", "goal", "owns", "acceptance")
MAX_SUMMARY_CHARS = 400
DEFAULT_TASK_BUDGET = 60_000
BUDGET_WARNING_RATIO = 0.8


class WorkflowError(Exception):
    """A request the workflow refuses: bad input, wrong state, or over budget."""


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.artifact_dir = self.root / "artifacts"
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "workflow.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # --- runs -------------------------------------------------------------------------

    def start_run(self, request, budget=500_000, max_fix_loops=2, repo=None, gate_commands=()):
        run_id = "run-" + uuid.uuid4().hex[:8]
        with self.db:
            self.db.execute(
                "INSERT INTO runs (id, request, budget, max_fix_loops, repo, gate_commands, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, request, budget, max_fix_loops, repo, json.dumps(list(gate_commands)), time.time()),
            )
        return run_id

    def run_config(self, run_id):
        run = self._run(run_id)
        return {"request": run["request"], "repo": run["repo"],
                "gate_commands": json.loads(run["gate_commands"]),
                "max_fix_loops": run["max_fix_loops"], "budget": run["budget"]}

    def task_state(self, run_id, task_id):
        return self._task(run_id, task_id)["state"]

    def _run(self, run_id):
        row = self.db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise WorkflowError(f"unknown run {run_id}")
        return row

    def set_repo_map(self, run_id, content):
        self._run(run_id)
        artifact_id = self.put_artifact(run_id, "repo_map", content)
        with self.db:
            self.db.execute("UPDATE runs SET repo_map = ? WHERE id = ?", (artifact_id, run_id))
        return artifact_id

    def repo_map(self, run_id):
        return self._run(run_id)["repo_map"]

    # --- tasks ------------------------------------------------------------------------

    def create_task(self, run_id, spec):
        self._run(run_id)
        missing = [k for k in REQUIRED_SPEC_KEYS if not spec.get(k)]
        if missing:
            raise WorkflowError(f"task spec missing {', '.join(missing)}")
        spec = {"reads": [], "interfaces": [], "depends_on": [], "risk": "low", "tests_first": False,
                "reviewers": ["general"], "budget_tokens": DEFAULT_TASK_BUDGET, **spec}
        for dep in spec["depends_on"]:
            # Tasks are created in dependency order, which also rules out cycles.
            self._task(run_id, dep)
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO tasks (run_id, id, spec, state) VALUES (?, ?, ?, 'planned')",
                    (run_id, spec["id"], json.dumps(spec)),
                )
        except sqlite3.IntegrityError:
            raise WorkflowError(f"task {spec['id']} already exists") from None
        return spec

    def _task(self, run_id, task_id):
        row = self.db.execute(
            "SELECT * FROM tasks WHERE run_id = ? AND id = ?", (run_id, task_id)
        ).fetchone()
        if row is None:
            raise WorkflowError(f"unknown task {task_id}")
        return row

    def list_tasks(self, run_id, state=None):
        self._run(run_id)
        query, args = "SELECT * FROM tasks WHERE run_id = ?", [run_id]
        if state:
            query += " AND state = ?"
            args.append(state)
        tasks = []
        for r in self.db.execute(query + " ORDER BY rowid", args):
            spec = json.loads(r["spec"])
            tasks.append({"id": r["id"], "state": r["state"], "claimed_by": r["claimed_by"],
                          "fix_loops": r["fix_loops"], "goal": spec["goal"], "risk": spec["risk"],
                          "tests_first": spec["tests_first"], "depends_on": spec["depends_on"]})
        return tasks

    def replan(self, run_id, task_id, spec):
        """Replace an escalated task's spec and send it back to 'planned'."""
        task = self._task(run_id, task_id)
        if task["state"] != "escalated":
            raise WorkflowError(f"task {task_id} is {task['state']}, only escalated tasks can be replanned")
        old = json.loads(task["spec"])
        spec = {**old, **spec, "id": task_id}
        with self.db:
            self.db.execute(
                "UPDATE tasks SET spec = ?, state = 'planned', claimed_by = NULL, fix_loops = 0 "
                "WHERE run_id = ? AND id = ?",
                (json.dumps(spec), run_id, task_id),
            )
        return spec

    # --- claim / report ---------------------------------------------------------------

    def claim(self, run_id, task_id, role):
        """Lock a task for a role and return the context packet that role needs."""
        task = self._task(run_id, task_id)
        spec = json.loads(task["spec"])
        if task["claimed_by"]:
            raise WorkflowError(f"task {task_id} is already claimed by {task['claimed_by']}")
        if role not in CLAIMABLE.get(task["state"], set()):
            raise WorkflowError(f"role {role} cannot claim task {task_id} in state {task['state']}")
        for dep in spec["depends_on"]:
            if self._task(run_id, dep)["state"] != "done":
                raise WorkflowError(f"task {task_id} is waiting on {dep}")

        budget = self._budget_status(run_id, task_id, spec)
        if budget["task_remaining"] <= 0:
            raise WorkflowError(f"task {task_id} is over its token budget; escalate or replan")
        if budget["run_remaining"] <= 0:
            raise WorkflowError(f"run {run_id} is over its token budget")

        with self.db:
            self.db.execute(
                "UPDATE tasks SET claimed_by = ? WHERE run_id = ? AND id = ?", (role, run_id, task_id)
            )
        return {
            "run_id": run_id,
            "task_id": task_id,
            "role": role,
            "state": task["state"],
            "goal": spec["goal"],
            "owns": spec["owns"],
            "reads": spec["reads"],
            "interfaces": spec["interfaces"],
            "acceptance": spec["acceptance"],
            "reviewers": spec["reviewers"] if role == "reviewer" else None,
            "repo_map": self.repo_map(run_id),
            "inputs": self._latest_artifacts(run_id, task_id),
            "budget": budget,
        }

    def report(self, run_id, task_id, role, status, summary, artifacts=()):
        """Record a claimed role's result, charge the ledger and advance the task.

        Status meanings per role: test_writer/coder "done" = work finished; gate "done" =
        all checks pass, "failed" = a check failed; reviewer "done" = no confirmed findings,
        "failed" = confirmed findings (put them in an artifact). "escalate" always escalates.
        """
        task = self._task(run_id, task_id)
        if task["claimed_by"] != role:
            raise WorkflowError(f"task {task_id} is not claimed by {role}")
        self._check_result(status, summary, artifacts)
        self._record(run_id, task_id, role, status, summary, artifacts)

        next_state = "escalated" if status == "escalate" else NEXT_STATE.get((role, status), "escalated")
        fix_loops = task["fix_loops"]
        if next_state == "fixing":
            fix_loops += 1
            if fix_loops > self._run(run_id)["max_fix_loops"]:
                next_state = "escalated"
        with self.db:
            self.db.execute(
                "UPDATE tasks SET state = ?, claimed_by = NULL, fix_loops = ? WHERE run_id = ? AND id = ?",
                (next_state, fix_loops, run_id, task_id),
            )
        spec = json.loads(task["spec"])
        return {"state": next_state, "fix_loops": fix_loops,
                "budget": self._budget_status(run_id, task_id, spec)}

    def release(self, run_id, task_id):
        """Drop a claim without a result, e.g. after an agent crashed."""
        self._task(run_id, task_id)
        with self.db:
            self.db.execute(
                "UPDATE tasks SET claimed_by = NULL WHERE run_id = ? AND id = ?", (run_id, task_id)
            )

    def record_run_result(self, run_id, role, status, summary, artifacts=()):
        """Record work not tied to one task (scout, architect, integrator, scribe)."""
        self._run(run_id)
        self._check_result(status, summary, artifacts)
        if role in TASK_ROLES:
            raise WorkflowError(f"role {role} works on tasks; use claim and report")
        self._record(run_id, None, role, status, summary, artifacts)

    def _check_result(self, status, summary, artifacts):
        if status not in STATUSES:
            raise WorkflowError(f"status must be one of {sorted(STATUSES)}")
        if len(summary) > MAX_SUMMARY_CHARS:
            raise WorkflowError(
                f"summary is {len(summary)} chars, max {MAX_SUMMARY_CHARS}; put detail in an artifact"
            )
        for artifact_id in artifacts:
            if self.db.execute("SELECT 1 FROM artifacts WHERE id = ?", (artifact_id,)).fetchone() is None:
                raise WorkflowError(f"unknown artifact {artifact_id}")

    def _record(self, run_id, task_id, role, status, summary, artifacts):
        if role not in ROLES:
            raise WorkflowError(f"unknown role {role}")
        with self.db:
            self.db.execute(
                "INSERT INTO results (run_id, task_id, role, status, summary, artifacts, tokens, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, task_id, role, status, summary, json.dumps(list(artifacts)), 0, time.time()),
            )

    def _latest_artifacts(self, run_id, task_id):
        """Artifacts from the task's most recent result: what the previous step produced."""
        row = self.db.execute(
            "SELECT role, summary, artifacts FROM results WHERE run_id = ? AND task_id = ? "
            "ORDER BY seq DESC LIMIT 1",
            (run_id, task_id),
        ).fetchone()
        if row is None:
            return None
        return {"from": row["role"], "summary": row["summary"], "artifacts": json.loads(row["artifacts"])}

    def results(self, run_id, task_id=None):
        query, args = "SELECT * FROM results WHERE run_id = ?", [run_id]
        if task_id:
            query += " AND task_id = ?"
            args.append(task_id)
        return [
            {"task_id": r["task_id"], "role": r["role"], "status": r["status"], "summary": r["summary"],
             "artifacts": json.loads(r["artifacts"])}
            for r in self.db.execute(query + " ORDER BY seq", args)
        ]

    # --- ledger -----------------------------------------------------------------------

    def charge(self, run_id, role, tokens, task_id=None):
        """Book tokens a subagent actually used, as measured by whoever ran it. Agents can't
        see their own usage, so self-estimates were dropped: they were mostly 0."""
        self._run(run_id)
        if role not in ROLES:
            raise WorkflowError(f"unknown role {role}")
        if tokens < 0:
            raise WorkflowError("tokens cannot be negative")
        if task_id:
            self._task(run_id, task_id)
        with self.db:
            self.db.execute(
                "INSERT INTO usage (run_id, task_id, role, tokens, created) VALUES (?, ?, ?, ?, ?)",
                (run_id, task_id, role, tokens, time.time()),
            )

    # Old runs booked self-estimates on results.tokens; new results store 0 there.
    SPEND = ("SELECT run_id, task_id, role, tokens FROM usage UNION ALL "
             "SELECT run_id, task_id, role, tokens FROM results")

    def _spent(self, run_id, task_id=None):
        query, args = f"SELECT COALESCE(SUM(tokens), 0) FROM ({self.SPEND}) WHERE run_id = ?", [run_id]
        if task_id:
            query += " AND task_id = ?"
            args.append(task_id)
        return self.db.execute(query, args).fetchone()[0]

    def _budget_status(self, run_id, task_id, spec):
        run_budget = self._run(run_id)["budget"]
        task_spent = self._spent(run_id, task_id)
        run_spent = self._spent(run_id)
        task_budget = spec["budget_tokens"]
        return {
            "task_spent": task_spent,
            "task_remaining": task_budget - task_spent,
            "run_remaining": run_budget - run_spent,
            "warning": task_spent >= BUDGET_WARNING_RATIO * task_budget
                       or run_spent >= BUDGET_WARNING_RATIO * run_budget,
        }

    def ledger(self, run_id):
        run = self._run(run_id)
        by_role = {r[0]: r[1] for r in self.db.execute(
            f"SELECT role, SUM(tokens) FROM ({self.SPEND}) WHERE run_id = ? GROUP BY role "
            "HAVING SUM(tokens) > 0", (run_id,))}
        by_task = {r[0]: r[1] for r in self.db.execute(
            f"SELECT task_id, SUM(tokens) FROM ({self.SPEND}) WHERE run_id = ? AND task_id IS NOT NULL "
            "GROUP BY task_id", (run_id,))}
        spent = self._spent(run_id)
        return {"budget": run["budget"], "spent": spent, "remaining": run["budget"] - spent,
                "by_role": by_role, "by_task": by_task}

    # --- artifacts --------------------------------------------------------------------

    def put_artifact(self, run_id, kind, content, task_id=None):
        self._run(run_id)
        artifact_id = f"{kind}-{uuid.uuid4().hex[:8]}"
        data = content.encode()
        (self.artifact_dir / artifact_id).write_bytes(data)
        with self.db:
            self.db.execute(
                "INSERT INTO artifacts (id, run_id, task_id, kind, size, created) VALUES (?, ?, ?, ?, ?, ?)",
                (artifact_id, run_id, task_id, kind, len(data), time.time()),
            )
        return artifact_id

    def get_artifact(self, artifact_id, offset=0, length=None):
        """Read an artifact, or a byte slice of it so agents can page through large logs."""
        row = self.db.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        if row is None:
            raise WorkflowError(f"unknown artifact {artifact_id}")
        with open(self.artifact_dir / artifact_id, "rb") as f:
            f.seek(offset)
            data = f.read(length if length is not None else -1)
        return {"id": artifact_id, "kind": row["kind"], "size": row["size"], "offset": offset,
                "content": data.decode(errors="replace")}
