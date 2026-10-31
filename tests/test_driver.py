"""The Stage 2 driver with a fake `claude` binary and a real git repo."""

import json
import sys
from pathlib import Path

import pytest

from agent_workflow import driver


@pytest.fixture
def run(server, repo):
    from agent_workflow import server as mcp
    run_id = mcp.workflow_start(request="fix value", repo=str(repo),
                                gate_commands=[f"{sys.executable} check.py"])["run_id"]
    mcp.store.set_repo_map(run_id, "check.py reads value.txt")
    mcp.store.create_task(run_id, {"id": "T1", "goal": "make check pass", "owns": ["value.txt"],
                                   "acceptance": ["check.py exits 0"]})
    return run_id


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """Returns a function that sets the per-role script and reads back the calls made."""
    log, plan = tmp_path / "claude.log", tmp_path / "plan.json"
    wrapper = tmp_path / "claude"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {Path(__file__).parent / 'fake_claude.py'} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("AGENT_WORKFLOW_CLAUDE", str(wrapper))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("FAKE_CLAUDE_PLAN", str(plan))

    def script(steps):
        plan.write_text(json.dumps(steps))
        return lambda: [json.loads(line) for line in log.read_text().splitlines()]
    return script


def report(run, role, status="done", summary="ok", artifacts=()):
    return f"store.report({run!r}, 'T1', {role!r}, {status!r}, {summary!r}, {list(artifacts)!r})"


def test_drives_a_run_to_the_end(run, repo, fake_claude):
    from agent_workflow import server as mcp
    report_id = mcp.store.put_artifact(run, "report", "T1 fixed value.txt")
    calls = fake_claude({
        "coder": ["import subprocess; open('value.txt', 'w').write('fixed\\n'); "
                  "subprocess.run(['git', 'commit', '-qam', 'T1: fix'], check=True); "
                  + report(run, "coder")],
        "reviewer": [report(run, "reviewer", summary="clean")],
        "integrator": [f"gate.merge_worktree({str(repo)!r}, {run!r}, 'T1'); "
                       f"store.record_run_result({run!r}, 'integrator', 'done', 'merged')"],
        "scribe": [f"store.record_run_result({run!r}, 'scribe', 'done', 'report', [{report_id!r}])"],
    })

    assert driver.drive(run) == report_id
    assert (repo / "value.txt").read_text() == "fixed\n"
    # Each agent's usage is booked: 150 fresh + 850 cache reads at a tenth = 235.
    assert mcp.store.ledger(run)["by_role"] == {"coder": 235, "reviewer": 235,
                                                "integrator": 235, "scribe": 235}

    coder, *_, scribe = calls()
    assert Path(coder["cwd"]) == mcp.home / "worktrees" / run / "T1"
    argv = coder["argv"]
    option = lambda name: argv[argv.index(name) + 1]
    sandbox = json.loads(option("--settings"))["sandbox"]
    assert sandbox["enabled"] and sandbox["failIfUnavailable"] and not sandbox["allowUnsandboxedCommands"]
    assert option("--permission-mode") == "dontAsk" and option("--model") == "sonnet"
    assert "--strict-mcp-config" in argv and "check.py reads value.txt" in option("--append-system-prompt")
    denied = argv[argv.index("--disallowedTools") + 1:]
    assert "mcp__agent-workflow__task_claim" in denied and "WebFetch" in denied
    assert scribe["argv"][scribe["argv"].index("--model") + 1] == "haiku"


def test_agent_that_never_reports_is_retried_once(run, fake_claude):
    from agent_workflow import server as mcp
    fake_claude({"coder": ["pass", "pass"]})
    with pytest.raises(driver.DriverError, match="twice ended without reporting"):
        driver.drive(run)
    assert mcp.store.list_tasks(run)[0]["claimed_by"] is None


def test_escalation_stops_the_driver(run, fake_claude):
    fake_claude({"coder": [report(run, "coder", status="escalate", summary="needs a decision")]})
    with pytest.raises(driver.DriverError, match="no task can move: {'T1': 'escalated'}"):
        driver.drive(run)
