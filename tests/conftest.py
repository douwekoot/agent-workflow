import importlib
import subprocess

import pytest


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
