"""The Copilot and Gemini adapters, run for real in the sandbox against fake CLIs."""

import shutil

import pytest

from agent_workflow import external

pytestmark = pytest.mark.skipif(not shutil.which("bwrap"), reason="needs bubblewrap")

# What the fake CLIs report about their surroundings goes into their answer.
PROBE = """
if (echo x > probe.txt) 2>/dev/null; then W=writable; else W=readonly; fi
H=$(ls -A "$HOME" | tr '\\n' ',')
S=${AW_TEST_SECRET:-none}
"""
FAKE_COPILOT = "#!/bin/sh\n" + PROBE + """
echo "$*" | grep -q -- "--deny-tool=write" && D=denied || D=allowed
echo "$*" | grep -q -- "--model m1" && M=m1 || M=default
printf '%s\\n' '{"type":"session.mcp_servers_loaded","data":{}}'
printf '%s\\n' "{\\"type\\":\\"assistant.message\\",\\"data\\":{\\"content\\":\\"copilot $W write-$D model-$M home=$H secret=$S token=${COPILOT_GITHUB_TOKEN:-none}\\"}}"
printf '%s\\n' '{"type":"session.usage_checkpoint","data":{"providerModelMetrics":[{"metrics":{"usage":{"inputTokens":100,"outputTokens":5}}},{"metrics":{"usage":{"inputTokens":20,"outputTokens":1}}}]}}'
printf '%s\\n' '{"type":"result","exitCode":0,"usage":{"premiumRequests":2}}'
"""
FAKE_GEMINI = "#!/bin/sh\n" + PROBE + """
case "$*" in *"approval-mode plan"*) A=plan;; *"approval-mode yolo"*) A=yolo;; *) A=none;; esac
echo "warning: something on stdout first"
printf '%s\\n' "{\\"session_id\\":\\"s\\",\\"response\\":\\"gemini $W $A secret=$S\\",\\"stats\\":{\\"models\\":{\\"a\\":{\\"tokens\\":{\\"total\\":120}},\\"b\\":{\\"tokens\\":{\\"total\\":3}}}}}"
"""


@pytest.fixture
def work(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    for name, script in (("copilot", FAKE_COPILOT), ("gemini", FAKE_GEMINI)):
        path = work / name
        path.write_text(script)
        path.chmod(0o755)
        monkeypatch.setenv(f"AGENT_WORKFLOW_{name.upper()}", str(path))
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "tok123")
    monkeypatch.setenv("GEMINI_API_KEY", "key123")
    monkeypatch.setenv("AW_TEST_SECRET", "hunter2")
    return work


def test_copilot_read_only_and_isolated(work):
    result = external.run("copilot:m1", "hi", work)
    assert result.ok and (result.tokens, result.detail) == (126, "2 premium requests")
    assert "readonly write-denied model-m1" in result.text
    assert not (work / "probe.txt").exists()
    assert "secret=none" in result.text and "token=tok123" in result.text  # env cleared, token passed
    home = result.text.split("home=")[1].split(" ")[0].split(",")
    assert not {".ssh", ".claude", ".config", ".aws"} & set(home)


def test_copilot_write_mode_can_write_only_its_worktree(work):
    result = external.run("copilot", "hi", work, write=True)
    assert "writable write-allowed model-default" in result.text
    assert (work / "probe.txt").exists()


def test_gemini_modes_and_usage(work):
    read = external.run("gemini", "hi", work)
    assert read.ok and read.tokens == 123 and "readonly plan secret=none" in read.text
    write = external.run("gemini", "hi", work, write=True)
    assert "writable yolo" in write.text


def test_failures_do_not_raise(work, tmp_path):
    (work / "gemini").write_text('#!/bin/sh\necho \'{"error":{"message":"quota exceeded"},"stats":{}}\'\n')
    assert external.run("gemini", "hi", work).error == "gemini: quota exceeded"
    (work / "copilot").write_text('#!/bin/sh\necho \'{"type":"result","exitCode":1}\'\n')
    assert not external.run("copilot", "hi", work).ok
    (work / "copilot").write_text("#!/bin/sh\nsleep 5\n")
    assert "timed out" in external.run("copilot", "hi", work, timeout=1).error


def test_check_explains_what_is_missing(work, monkeypatch, tmp_path):
    assert external.check("copilot") == "" and external.check("gemini") == ""
    monkeypatch.setattr(external, "_copilot_token", lambda: "")
    assert "no GitHub token" in external.check("copilot")
    monkeypatch.delenv("GEMINI_API_KEY")
    monkeypatch.setattr(external, "HOME", tmp_path / "empty-home")
    assert "not signed in" in external.check("gemini")
    monkeypatch.setenv("AGENT_WORKFLOW_GEMINI", str(tmp_path / "missing"))
    assert "not found" in external.check("gemini")
    with pytest.raises(ValueError, match="unknown provider"):
        external.split_spec("bard")


def test_parse_findings():
    two = '```json\n{"claim": "a"}\nnot json\n{"claim": "b"}\n```'
    assert external.parse_findings(two) == [{"claim": "a"}, {"claim": "b"}]
    assert external.parse_findings("Looked fine.\nNO FINDINGS") == []
    assert external.parse_findings("I could not decide.") is None
