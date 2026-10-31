"""The Stage 2 driver with a fake `claude` binary and a real git repo."""

import json
import sys
from pathlib import Path

import pytest

from agent_workflow import driver


def make_run(repo, with_map=True, with_task=True, **task_extra):
    from agent_workflow import server as mcp
    run_id = mcp.workflow_start(request="fix value", repo=str(repo),
                                gate_commands=[f"{sys.executable} check.py"])["run_id"]
    if with_map:
        mcp.store.set_repo_map(run_id, "check.py reads value.txt")
    if with_task:
        mcp.store.create_task(run_id, {"id": "T1", "goal": "make check pass", "owns": ["value.txt"],
                                       "acceptance": ["check.py exits 0"], **task_extra})
    return run_id


@pytest.fixture
def run(server, repo):
    return make_run(repo)


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


# --- external providers (--use) ---------------------------------------------------------

CODER = ("import subprocess; open('value.txt', 'w').write('fixed\\n'); "
         "subprocess.run(['git', 'commit', '-qam', 'T1: fix'], check=True); ")


def finish_steps(run, repo, report_id=None):
    """Claude's reviewer, integrator and scribe for a task that passed the gate."""
    return {
        "reviewer": [report(run, "reviewer", summary="clean")],
        "integrator": [f"gate.merge_worktree({str(repo)!r}, {run!r}, 'T1'); "
                       f"store.record_run_result({run!r}, 'integrator', 'done', 'merged')"],
        "scribe": [f"store.record_run_result({run!r}, 'scribe', 'done', 'report', "
                   f"{[report_id] if report_id else []!r})"],
    }


@pytest.fixture
def providers(monkeypatch):
    """Replaces external.run: returns the calls made, answering from a per-spec script."""
    from agent_workflow import external
    calls, script = [], {}

    def fake(spec, prompt, cwd, write=False, timeout=0):
        calls.append({"spec": spec, "prompt": prompt, "cwd": Path(cwd), "write": write})
        return script[spec](Path(cwd))

    monkeypatch.setattr(external, "run", fake)
    return calls, script


def ok(text="done", tokens=50):
    from agent_workflow import external
    return external.Result(True, text, tokens)


def test_provider_writes_tests_and_code_inside_its_scope(server, repo, fake_claude, providers):
    from agent_workflow import server as mcp
    calls, script = providers

    def test_writer(wt):
        (wt / "tests").mkdir()
        (wt / "tests/test_value.py").write_text("def test_value(): pass\n")
        (wt / "notes.md").write_text("not a test\n")  # out of scope for a test writer
        return ok("wrote tests/test_value.py", 50)

    def coder(wt):
        (wt / "value.txt").write_text("fixed\n")
        (wt / "extra.py").write_text("print('not owned')\n")  # out of scope for the coder
        return ok("fixed value.txt", 70)

    script["copilot"], script["gemini"] = test_writer, coder
    run = make_run(repo, risk="low", tests_first=True)
    calls_made = fake_claude(finish_steps(run, repo))

    driver.drive(run, uses={"test_writer": "copilot", "coder_low": "gemini"})

    assert [c["spec"] for c in calls] == ["copilot", "gemini"] and all(c["write"] for c in calls)
    assert "coder" not in {c["role"] for c in calls_made()}  # Claude never coded
    assert (repo / "value.txt").read_text() == "fixed\n"
    assert not (repo / "notes.md").exists() and not (repo / "extra.py").exists()
    assert (repo / "tests/test_value.py").exists()
    ledger = mcp.store.ledger(run)
    assert ledger["external"] == {"copilot": 50, "gemini": 70}
    assert ledger["by_role"].keys() == {"reviewer", "integrator", "scribe"}
    assert "owns" in calls[1]["prompt"] and "make check pass" in calls[1]["prompt"]


def test_failed_provider_falls_back_to_a_claude_agent(server, repo, fake_claude, providers):
    from agent_workflow import external
    calls, script = providers

    def broken(wt):
        (wt / "junk.txt").write_text("half done\n")
        return external.Result(False, error="quota exceeded")

    script["gemini"] = broken
    run = make_run(repo, risk="low")
    steps = finish_steps(run, repo)
    steps["coder"] = ["import os; assert not os.path.exists('junk.txt'), 'worktree not reset'; "
                      + CODER + report(run, "coder")]
    claude_calls = fake_claude(steps)

    driver.drive(run, uses={"coder_low": "gemini"})
    assert (repo / "value.txt").read_text() == "fixed\n" and not (repo / "junk.txt").exists()
    assert "coder" in {c["role"] for c in claude_calls()}


def test_cheap_coder_only_gets_the_first_attempt(server, repo, fake_claude, providers):
    calls, script = providers
    script["gemini"] = lambda wt: ok()
    run = make_run(repo, risk="low")
    fake_claude({"coder": [CODER + report(run, "coder")]})
    from agent_workflow import server as mcp
    mcp.store.claim(run, "T1", "coder")
    mcp.store.report(run, "T1", "coder", "done", "first try")
    mcp.store.claim(run, "T1", "gate")
    mcp.store.report(run, "T1", "gate", "failed", "tests fail")
    task = mcp.store.list_tasks(run)[0]
    assert driver.external_choice(task, "coder", {"coder_low": "gemini"}) is None
    assert driver.external_choice({**task, "fix_loops": 0, "risk": "medium"}, "coder", {"coder_low": "gemini"}) is None
    assert driver.external_choice({**task, "fix_loops": 0}, "coder", {"coder_low": "gemini"}) == "gemini"


def test_second_opinion_goes_to_the_claude_reviewer(server, repo, fake_claude, providers):
    calls, script = providers
    script["gemini"] = lambda wt: ok('{"file": "value.txt", "line": 1, "severity": "low", '
                                     '"claim": "c", "scenario": "s"}')
    run = make_run(repo, risk="high")
    steps = finish_steps(run, repo)
    steps["coder"] = [CODER + report(run, "coder")]
    claude_calls = fake_claude(steps)

    driver.drive(run, uses={"second_opinion": "gemini"})
    (call,) = calls
    assert not call["write"] and "+fixed" in call["prompt"] and "acceptance" in call["prompt"]
    reviewer = next(c for c in claude_calls() if c["role"] == "reviewer")
    assert "second_opinion-" in reviewer["argv"][1]
    assert reviewer["argv"][reviewer["argv"].index("--model") + 1] == "opus"


def test_plan_critic_makes_the_architect_revise_once(server, repo, fake_claude, providers):
    from agent_workflow import server as mcp
    calls, script = providers
    script["gemini"] = lambda wt: ok('{"task": "T1", "severity": "high", "issue": "needs a test", '
                                     '"suggestion": "own tests/test_value.py"}')
    run = make_run(repo)
    steps = finish_steps(run, repo)
    steps["architect"] = [f"store.replan({run!r}, 'T1', {{'owns': ['value.txt', 'tests/test_value.py']}}); "
                          f"store.record_run_result({run!r}, 'architect', 'done', 'revised')"]
    steps["coder"] = [CODER + report(run, "coder")]
    claude_calls = fake_claude(steps)

    driver.drive(run, uses={"plan_critic": "gemini"})
    assert mcp.store.specs(run)[0]["owns"] == ["value.txt", "tests/test_value.py"]
    architect = next(c for c in claude_calls() if c["role"] == "architect")
    assert '"revise": true' in architect["argv"][1] and "plan_critique-" in architect["argv"][1]
    assert "mcp__agent-workflow__task_replan" not in architect["argv"][architect["argv"].index("--disallowedTools"):]
    assert [r["status"] for r in mcp.store.results(run) if r["role"] == "critic"] == ["done"]
    assert len(calls) == 1 and "T1" in calls[0]["prompt"] and not calls[0]["write"]


def test_plan_critic_with_no_findings_leaves_the_plan(server, repo, fake_claude, providers):
    calls, script = providers
    script["gemini"] = lambda wt: ok("The plan is sound.\nNO FINDINGS")
    run = make_run(repo)
    steps = finish_steps(run, repo)
    steps["coder"] = [CODER + report(run, "coder")]
    claude_calls = fake_claude(steps)
    driver.drive(run, uses={"plan_critic": "gemini"})
    assert "architect" not in {c["role"] for c in claude_calls()}


def test_provider_scout_and_scribe(server, repo, fake_claude, providers):
    from agent_workflow import server as mcp
    calls, script = providers
    repo_map = "check.py and value.txt; run python3 check.py. " * 10
    script["gemini"] = lambda wt: ok(repo_map, 10)
    script["copilot"] = lambda wt: ok("REPORT: T1 done", 20)
    run = make_run(repo, with_map=False, with_task=False)
    steps = finish_steps(run, repo)
    steps["architect"] = [f"store.create_task({run!r}, {{'id': 'T1', 'goal': 'g', 'owns': ['value.txt'], "
                          f"'acceptance': ['a']}}); store.record_run_result({run!r}, 'architect', 'done', 'planned')"]
    steps["coder"] = [CODER + report(run, "coder")]
    del steps["scribe"]
    claude_calls = fake_claude(steps)

    report_id = driver.drive(run, uses={"scout": "gemini", "scribe": "copilot"})
    assert mcp.store.get_artifact(mcp.store.repo_map(run))["content"] == repo_map
    assert mcp.store.get_artifact(report_id)["content"] == "REPORT: T1 done"
    assert {c["role"] for c in claude_calls()}.isdisjoint({"scout", "scribe"})
    assert mcp.store.ledger(run)["external"] == {"gemini": 10, "copilot": 20}


def test_unusable_scout_map_falls_back_to_claude(server, repo, fake_claude, providers):
    calls, script = providers
    script["gemini"] = lambda wt: ok("too short")
    run = make_run(repo, with_map=False)
    steps = finish_steps(run, repo)
    steps["scout"] = [f"store.set_repo_map({run!r}, 'claude map'); "
                      f"store.record_run_result({run!r}, 'scout', 'done', 'mapped')"]
    steps["coder"] = [CODER + report(run, "coder")]
    claude_calls = fake_claude(steps)
    driver.drive(run, uses={"scout": "gemini"})
    assert "scout" in {c["role"] for c in claude_calls()}


def test_parse_uses():
    from agent_workflow import delegate
    assert delegate.parse_uses(["scout=gemini", "coder_low=copilot:gpt-5"]) == {
        "scout": "gemini", "coder_low": "copilot:gpt-5"}
    for bad in ("scout", "magic=gemini", "scout=bard"):
        with pytest.raises(ValueError):
            delegate.parse_uses([bad])
