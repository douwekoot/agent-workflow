"""Deterministic checks and git worktrees: the parts of the workflow that need no LLM."""

import shlex
import shutil
import subprocess
from pathlib import Path

SECRET_DIRS = (".ssh", ".aws", ".gnupg")


def sandboxed(argv, cwd):
    """Gate commands run code the agents wrote. Under bubblewrap they get no network, can
    write only inside the worktree (plus a private /tmp) and can't see secrets dirs.
    Returns (argv, sandboxed); without bwrap (e.g. macOS) the command runs as is."""
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return argv, False
    home = Path.home()
    hide = [arg for d in SECRET_DIRS if (home / d).exists() for arg in ("--tmpfs", str(home / d))]
    cwd = str(Path(cwd).resolve())
    return [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
            *hide, "--bind", cwd, cwd, "--chdir", cwd, "--unshare-net", "--die-with-parent",
            *argv], True


def run_gate(cwd, commands, tail_lines=60, timeout=600):
    """Run commands in order, sandboxed where possible, and stop at the first failure.

    Only the tail of the failing command's output is returned, so the coder that fixes it
    reads the error rather than the whole build log.
    """
    is_sandboxed = shutil.which("bwrap") is not None
    for command in commands:
        argv, is_sandboxed = sandboxed(shlex.split(command), cwd)
        try:
            proc = subprocess.run(
                argv, cwd=cwd, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            return {"passed": False, "failed_command": command, "sandboxed": is_sandboxed,
                    "log": f"timed out after {timeout}s"}
        except FileNotFoundError as e:
            return {"passed": False, "failed_command": command, "sandboxed": is_sandboxed,
                    "log": str(e)}
        if proc.returncode != 0:
            output = (proc.stdout + proc.stderr).splitlines()
            log = "\n".join(output[-tail_lines:])
            if len(output) > tail_lines:
                log = f"[... {len(output) - tail_lines} earlier lines cut ...]\n" + log
            return {"passed": False, "failed_command": command, "sandboxed": is_sandboxed,
                    "log": f"exit code {proc.returncode}\n{log}"}
    return {"passed": True, "failed_command": None, "sandboxed": is_sandboxed, "log": ""}


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def current_branch(repo):
    proc = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())
    return proc.stdout.strip()


def check_identity(repo):
    """Agents commit in worktrees; fail at the start of a run, not in the first coder."""
    proc = _git(repo, "var", "GIT_AUTHOR_IDENT")
    if proc.returncode != 0:
        raise RuntimeError(
            f"git has no commit identity for {repo}; set one with "
            f"git -C {repo} config user.name '...' and git -C {repo} config user.email '...'"
        )


def branch_name(run_id, task_id):
    return f"aw/{run_id}/{task_id}"


def create_worktree(repo, worktree_root, run_id, task_id, base="HEAD"):
    """Give a task its own branch and working directory so parallel coders never collide."""
    path = Path(worktree_root) / run_id / task_id
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = _git(repo, "worktree", "add", "-b", branch_name(run_id, task_id), str(path), base)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())
    return path


def merge_worktree(repo, run_id, task_id):
    """Merge a finished task's branch into the repo's current branch.

    On a conflict the merge is aborted and the conflicting output is returned for the
    integrator, so the repo is never left half-merged.
    """
    branch = branch_name(run_id, task_id)
    proc = _git(repo, "merge", "--no-ff", "-m", f"Merge {branch}", branch)
    if proc.returncode != 0:
        _git(repo, "merge", "--abort")
        return {"merged": False, "log": (proc.stdout + proc.stderr).strip()}
    return {"merged": True, "log": proc.stdout.strip()}


def remove_worktree(repo, path):
    proc = _git(repo, "worktree", "remove", "--force", str(path))
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())
