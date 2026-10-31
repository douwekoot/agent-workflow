"""The Stage 2 driver against a scripted fake API client and a real git repo."""

import contextlib
import itertools
import re
import sys
from types import SimpleNamespace

import pytest

from agent_workflow import driver

ids = itertools.count()


def tool(name, **input):
    return SimpleNamespace(type="tool_use", id=f"toolu_{next(ids)}", name=name, input=input)


def text(value):
    return SimpleNamespace(type="text", text=value)


def reply(*blocks):
    stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
    usage = SimpleNamespace(input_tokens=100, output_tokens=50, cache_creation_input_tokens=0,
                            cache_read_input_tokens=850)
    return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=usage)


class FakeClient:
    """Plays back one scripted list of responses per role, recording every request."""

    def __init__(self, scripts):
        self.scripts = scripts
        self.requests = []
        self.beta = SimpleNamespace(messages=self)

    def stream(self, **request):
        self.requests.append(request)
        role = re.search(r"# Role: (.+)", request["system"][1]["text"]).group(1)
        response = self.scripts[role].pop(0)
        return contextlib.nullcontext(SimpleNamespace(get_final_message=lambda: response))


@pytest.fixture
def run(server, repo):
    from agent_workflow import server as mcp
    run_id = mcp.workflow_start(request="fix value", repo=str(repo),
                                gate_commands=[f"{sys.executable} check.py"])["run_id"]
    mcp.store.set_repo_map(run_id, "check.py reads value.txt")
    mcp.store.create_task(run_id, {"id": "T1", "goal": "make check pass", "owns": ["value.txt"],
                                   "acceptance": ["check.py exits 0"]})
    return run_id


def report(run, role, status="done", summary="ok", **extra):
    return reply(tool("task_report", run_id=run, task_id="T1", role=role, status=status,
                      summary=summary, **extra))


def test_drives_a_run_to_the_end(run, repo):
    from agent_workflow import server as mcp
    report_id = mcp.store.put_artifact(run, "report", "T1 fixed value.txt")
    client = FakeClient({
        "coder": [
            reply(tool(driver.EDITOR, command="str_replace", path="value.txt",
                       old_str="broken", new_str="fixed"),
                  tool(driver.EDITOR, command="view", path="../../escape.txt")),
            reply(tool("bash", command="git commit -qam 'T1: fix value'")),
            report(run, "coder", summary="set value to fixed"),
            reply(text("set value to fixed")),
        ],
        "reviewer": [report(run, "reviewer", summary="clean"), reply(text("clean"))],
        "integrator": [
            reply(tool("task_merge", run_id=run, task_id="T1")),
            reply(tool("run_report", run_id=run, role="integrator", status="done", summary="merged")),
            reply(text("merged")),
        ],
        "scribe": [
            reply(tool("run_report", run_id=run, role="scribe", status="done", summary="report",
                       artifacts=[report_id])),
            reply(text("report")),
        ],
    })

    assert driver.drive(client, run) == report_id
    assert (repo / "value.txt").read_text() == "fixed\n"
    # Every response's usage is booked: 1000 tokens per response in the fake.
    assert mcp.store.ledger(run)["by_role"] == {"coder": 4000, "reviewer": 2000,
                                                "integrator": 3000, "scribe": 2000}

    first = client.requests[0]
    assert [b.get("cache_control") for b in first["system"]] == [{"type": "ephemeral"}] * 2
    assert "check.py reads value.txt" in first["system"][0]["text"]
    assert first["model"] == "claude-sonnet-5-5" and first["fallbacks"] == "default"
    names = [t["name"] for t in first["tools"]]
    assert names[:2] == ["bash", driver.EDITOR] and names[2:] == sorted(names[2:])
    assert "task_report" in names and not driver.DRIVER_ONLY & set(names)
    # Identical tools on every request keep the cached prefix shared across agents.
    assert all(r["tools"] == first["tools"] for r in client.requests)
    scribe = client.requests[-1]
    assert scribe["model"] == "claude-haiku-5-5" and "fallbacks" not in scribe

    edit, escape = client.requests[0]["messages"][2]["content"]  # results of the first turn
    assert edit["is_error"] is False
    assert escape["is_error"] is True and "outside" in escape["content"]


def test_agent_that_never_reports_is_retried_once(run):
    from agent_workflow import server as mcp
    client = FakeClient({"coder": [reply(text("done?")), reply(text("done?"))]})
    with pytest.raises(driver.DriverError, match="twice ended without reporting"):
        driver.drive(client, run)
    assert mcp.store.list_tasks(run)[0]["claimed_by"] is None


def test_escalation_stops_the_driver(run):
    client = FakeClient({"coder": [report(run, "coder", status="escalate", summary="needs a decision"),
                                   reply(text("escalated"))]})
    with pytest.raises(driver.DriverError, match="no task can move: {'T1': 'escalated'}"):
        driver.drive(client, run)
