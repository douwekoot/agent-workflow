import pytest

from agent_workflow.core import Store, WorkflowError


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


@pytest.fixture
def run(store):
    return store.start_run("add retries", budget=100_000, max_fix_loops=2)


def spec(task_id, **extra):
    return {"id": task_id, "goal": f"do {task_id}", "owns": [f"{task_id}.py"],
            "acceptance": ["works"], **extra}


def step(store, run, task_id, role, status, tokens=0, artifacts=()):
    store.claim(run, task_id, role)
    if tokens:
        store.charge(run, role, tokens, task_id)
    return store.report(run, task_id, role, status, "ok", list(artifacts))


def test_happy_path_with_test_writer(store, run):
    store.create_task(run, spec("T1"))
    assert step(store, run, "T1", "test_writer", "done")["state"] == "tests_written"
    assert step(store, run, "T1", "coder", "done")["state"] == "coded"
    assert step(store, run, "T1", "gate", "done")["state"] == "reviewing"
    assert step(store, run, "T1", "reviewer", "done")["state"] == "done"


def test_roles_cannot_skip_steps(store, run):
    store.create_task(run, spec("T1"))
    with pytest.raises(WorkflowError, match="cannot claim"):
        store.claim(run, "T1", "reviewer")
    step(store, run, "T1", "coder", "done")
    with pytest.raises(WorkflowError, match="cannot claim"):
        store.claim(run, "T1", "reviewer")


def test_report_requires_matching_claim(store, run):
    store.create_task(run, spec("T1"))
    with pytest.raises(WorkflowError, match="not claimed"):
        store.report(run, "T1", "coder", "done", "ok")
    store.claim(run, "T1", "coder")
    with pytest.raises(WorkflowError, match="already claimed"):
        store.claim(run, "T1", "coder")


def test_fix_loop_cap_escalates(store, run):
    store.create_task(run, spec("T1"))
    step(store, run, "T1", "coder", "done")
    assert step(store, run, "T1", "gate", "failed")["state"] == "fixing"
    step(store, run, "T1", "coder", "done")
    step(store, run, "T1", "gate", "done")
    assert step(store, run, "T1", "reviewer", "failed")["state"] == "fixing"
    step(store, run, "T1", "coder", "done")
    result = step(store, run, "T1", "gate", "failed")
    assert result == {**result, "state": "escalated", "fix_loops": 3}


def test_escalate_and_replan(store, run):
    store.create_task(run, spec("T1"))
    assert step(store, run, "T1", "coder", "escalate")["state"] == "escalated"
    new = store.replan(run, "T1", {"goal": "smaller goal"})
    assert new["goal"] == "smaller goal" and new["owns"] == ["T1.py"]
    assert store.list_tasks(run)[0]["state"] == "planned"
    with pytest.raises(WorkflowError, match="only escalated"):
        store.replan(run, "T1", {})


def test_dependencies_block_claims(store, run):
    store.create_task(run, spec("T1"))
    store.create_task(run, spec("T2", depends_on=["T1"]))
    with pytest.raises(WorkflowError, match="waiting on T1"):
        store.claim(run, "T2", "coder")
    for role in ("coder", "gate", "reviewer"):
        step(store, run, "T1", role, "done")
    store.claim(run, "T2", "coder")


def test_dependencies_must_exist(store, run):
    with pytest.raises(WorkflowError, match="unknown task"):
        store.create_task(run, spec("T2", depends_on=["T1"]))


def test_spec_validation(store, run):
    with pytest.raises(WorkflowError, match="missing owns, acceptance"):
        store.create_task(run, {"id": "T1", "goal": "x"})
    store.create_task(run, spec("T1"))
    with pytest.raises(WorkflowError, match="already exists"):
        store.create_task(run, spec("T1"))


def test_task_budget_refuses_claims(store, run):
    store.create_task(run, spec("T1", budget_tokens=1000))
    result = step(store, run, "T1", "coder", "done", tokens=850)
    assert result["budget"]["warning"] is True
    step(store, run, "T1", "gate", "failed", tokens=200)
    with pytest.raises(WorkflowError, match="over its token budget"):
        store.claim(run, "T1", "coder")


def test_run_budget_refuses_claims(store):
    run = store.start_run("big", budget=1000)
    store.charge(run, "architect", 1000)
    store.create_task(run, spec("T1"))
    with pytest.raises(WorkflowError, match="run .* is over its token budget"):
        store.claim(run, "T1", "coder")


def test_ledger(store, run):
    store.create_task(run, spec("T1"))
    store.charge(run, "scout", 3000)
    step(store, run, "T1", "coder", "done", tokens=5000)
    store.charge(run, "coder", 1000, "T1")  # a coder that died without reporting
    ledger = store.ledger(run)
    assert ledger["spent"] == 9000
    assert ledger["remaining"] == 91_000
    assert ledger["by_role"] == {"scout": 3000, "coder": 6000}
    assert ledger["by_task"] == {"T1": 6000}
    with pytest.raises(WorkflowError, match="unknown task"):
        store.charge(run, "coder", 10, "T9")
    with pytest.raises(WorkflowError, match="negative"):
        store.charge(run, "coder", -1, "T1")


def test_result_limits(store, run):
    store.create_task(run, spec("T1"))
    store.claim(run, "T1", "coder")
    with pytest.raises(WorkflowError, match="max 400"):
        store.report(run, "T1", "coder", "done", "x" * 401)
    with pytest.raises(WorkflowError, match="unknown artifact"):
        store.report(run, "T1", "coder", "done", "ok", ["nope"])
    with pytest.raises(WorkflowError, match="status must be"):
        store.report(run, "T1", "coder", "great", "ok")


def test_task_roles_must_use_claims(store, run):
    with pytest.raises(WorkflowError, match="use claim and report"):
        store.record_run_result(run, "coder", "done", "ok")
    with pytest.raises(WorkflowError, match="unknown role"):
        store.record_run_result(run, "wizard", "done", "ok")


def test_packet_passes_previous_step_artifacts(store, run):
    store.set_repo_map(run, "src/ -- everything")
    store.create_task(run, spec("T1", reads=["types.py"]))
    store.claim(run, "T1", "coder")
    diff = store.put_artifact(run, "diff", "+retry", "T1")
    store.report(run, "T1", "coder", "done", "added retry", [diff])
    packet = store.claim(run, "T1", "gate")
    assert packet["inputs"] == {"from": "coder", "summary": "added retry", "artifacts": [diff]}
    assert packet["reads"] == ["types.py"]
    assert store.get_artifact(packet["repo_map"])["content"] == "src/ -- everything"


def test_artifact_slices(store, run):
    artifact = store.put_artifact(run, "log", "0123456789")
    part = store.get_artifact(artifact, offset=3, length=4)
    assert part["content"] == "3456" and part["size"] == 10


def test_state_survives_reopen(tmp_path):
    first = Store(tmp_path)
    run = first.start_run("persist")
    first.create_task(run, spec("T1"))
    assert Store(tmp_path).list_tasks(run)[0]["id"] == "T1"
