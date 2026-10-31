import os
import shutil
import sys
from pathlib import Path

import pytest

from agent_workflow import gate


@pytest.mark.skipif(not shutil.which("bwrap"), reason="needs bubblewrap")
def test_gate_commands_are_sandboxed(tmp_path):
    worktree, outside = tmp_path / "wt", tmp_path / "outside.txt"
    worktree.mkdir()
    write = f"{sys.executable} -c \"open('{{}}', 'w').write('x')\""
    assert gate.run_gate(worktree, [write.format(worktree / "ok.txt")]) == {
        "passed": True, "failed_command": None, "sandboxed": True, "log": ""}
    assert (worktree / "ok.txt").exists()

    # /tmp is a private tmpfs in the sandbox: the write succeeds but never reaches the host.
    gate.run_gate(worktree, [write.format(outside)])
    assert not outside.exists()
    home_file = Path.home() / f".aw-gate-escape-{os.getpid()}"
    escaped = gate.run_gate(worktree, [write.format(home_file)])
    assert escaped["passed"] is False and "Read-only file system" in escaped["log"]
    assert not home_file.exists()

    connect = f"{sys.executable} -c \"import socket; socket.create_connection(('1.1.1.1', 53), 3)\""
    assert gate.run_gate(worktree, [connect])["passed"] is False
