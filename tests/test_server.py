"""End-to-end: drive a whole task through the MCP tools against a real git repo."""

import asyncio
import importlib
import json
import subprocess
import sys

import pytest
from mcp.server.mcpserver.exceptions import ToolError


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKFLOW_HOME", str(tmp_path / "state"))
    for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(key, "test")
    for key in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, "test@example.com")
    import agent_workflow.server as module
    return importlib.reload(module).server


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    # The "test suite": passes only once value.txt says "fixed".
    (path / "check.py").write_text(
        "import sys\nok = open('value.txt').read().strip() == 'fixed'\n"
        "print('value ok' if ok else 'value is wrong')\nsys.exit(0 if ok else 1)\n"
    )
    (path / "value.txt").write_text("broken\n")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "init")
    return path


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def call(server, name, **arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    assert not result.is_error
    return json.loads(result.content[0].text)


def call_error(server, name, **arguments):
    with pytest.raises(ToolError) as info:
        asyncio.run(server.call_tool(name, arguments))
    return str(info.value)


def commit_value(worktree, value):
    (worktree / "value.txt").write_text(value + "\n")
    git(worktree, "commit", "-q", "-am", f"set {value}")


def test_full_task_through_mcp(server, repo):
    from pathlib import Path

    run = call(server, "workflow_start", request="fix value", repo=str(repo),
               gate_commands=[f"{sys.executable} check.py"])["run_id"]
    call(server, "repo_map_put", run_id=run, content="check.py, value.txt")
    call(server, "task_create", run_id=run,
         spec={"id": "T1", "goal": "make check pass", "owns": ["value.txt"], "acceptance": ["check.py exits 0"]})

    # First attempt is wrong: the gate fails and the coder gets only the failing tail.
    claim = call(server, "task_claim", run_id=run, task_id="T1", role="coder")
    assert "# Role: coder" in claim["instructions"] and "subagent rules" in claim["instructions"]
    assert claim["packet"]["base_branch"] == "main"
    worktree = Path(claim["packet"]["worktree"])
    commit_value(worktree, "still broken")
    call(server, "task_report", run_id=run, task_id="T1", role="coder", status="done",
         summary="tried")
    call(server, "usage_report", run_id=run, role="coder", task_id="T1", tokens=1000)
    gate = call(server, "gate_run", run_id=run, task_id="T1")
    assert gate["passed"] is False and gate["state"] == "fixing"
    log = call(server, "artifact_get", artifact_id=gate["artifacts"][0])
    assert "value is wrong" in log["content"]

    packet = call(server, "task_claim", run_id=run, task_id="T1", role="coder")["packet"]
    assert packet["inputs"]["artifacts"] == gate["artifacts"]
    commit_value(worktree, "fixed")
    call(server, "task_report", run_id=run, task_id="T1", role="coder", status="done",
         summary="fixed")
    call(server, "usage_report", run_id=run, role="coder", task_id="T1", tokens=500)
    assert call(server, "gate_run", run_id=run, task_id="T1")["state"] == "reviewing"

    call(server, "task_claim", run_id=run, task_id="T1", role="reviewer")
    assert call(server, "task_report", run_id=run, task_id="T1", role="reviewer", status="done",
                summary="clean")["state"] == "done"
    call(server, "usage_report", run_id=run, role="reviewer", task_id="T1", tokens=800)

    assert call(server, "task_merge", run_id=run, task_id="T1")["merged"] is True
    assert (repo / "value.txt").read_text() == "fixed\n"
    assert call(server, "ledger_get", run_id=run)["by_role"] == {"coder": 1500, "reviewer": 800}
    roles = [r["role"] for r in call(server, "results_get", run_id=run, task_id="T1")["results"]]
    assert roles == ["coder", "gate", "coder", "gate", "reviewer"]


def test_role_brief_and_orchestrate_prompt(server, repo):
    run = call(server, "workflow_start", request="add retries", repo=str(repo),
               gate_commands=["make test"])["run_id"]
    brief = call(server, "role_brief", run_id=run, role="architect")
    assert "# Role: architect" in brief["instructions"]
    assert brief["run"]["request"] == "add retries" and brief["run"]["tasks"] is None
    assert "task_claim" in call_error(server, "role_brief", run_id=run, role="coder")

    prompt = asyncio.run(server.get_prompt("orchestrate", {"request": "add retries"}))
    text = prompt.messages[0].content.text
    assert "Request: add retries" in text
    assert "run_id={run_id}" in text  # spawn templates survive formatting


def test_every_role_has_instructions():
    from agent_workflow.server import RUN_ROLES, instructions
    from agent_workflow.core import TASK_ROLES

    for role in (TASK_ROLES - {"gate"}) | set(RUN_ROLES):
        assert f"# Role: {role.replace('_', ' ')}" in instructions(role)


def test_tool_errors_are_reported(server, repo, tmp_path):
    assert "not a git repository" in call_error(
        server, "workflow_start", request="x", repo=str(tmp_path), gate_commands=[])
    run = call(server, "workflow_start", request="x", repo=str(repo), gate_commands=[])["run_id"]
    call(server, "task_create", run_id=run,
         spec={"id": "T1", "goal": "g", "owns": ["a"], "acceptance": ["b"]})
    assert "use gate_run" in call_error(server, "task_claim", run_id=run, task_id="T1", role="gate")
    assert "not done" in call_error(server, "task_merge", run_id=run, task_id="T1")


def test_start_needs_git_identity(server, repo, monkeypatch, tmp_path):
    for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", str(tmp_path))  # no global git config
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    git(repo, "config", "user.useConfigOnly", "true")
    assert "no commit identity" in call_error(
        server, "workflow_start", request="x", repo=str(repo), gate_commands=[])


def test_stdio_server_lists_tools(tmp_path):
    """Start the real server process and talk to it over stdio like a client would."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable, args=["-m", "agent_workflow.server"],
        env={"AGENT_WORKFLOW_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )

    async def list_tools():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return {tool.name for tool in (await session.list_tools()).tools}

    names = asyncio.run(list_tools())
    assert {"workflow_start", "task_claim", "task_report", "gate_run", "ledger_get"} <= names
