"""Other vendors' agent CLIs, run headless: GitHub Copilot and Gemini.

They serve as second opinions and cheap workers. They get no workflow tools. The driver
hands them a prompt and takes their text back, then does all the bookkeeping itself.

Each call runs in a bubblewrap sandbox. They need the network to reach their models, so
the sandbox instead hides everything else: HOME is an empty tmpfs holding only the CLI
install and the provider's own login directory, the environment is cleared, the session
bus is hidden, and the working directory is read-only unless the job writes code.
The provider's login directory stays readable and writable, so a prompt-injected agent
could read that provider's token.

A provider spec is "copilot", "gemini", or either with a model: "gemini:gemini-3-pro".
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

PROVIDERS = ("copilot", "gemini")
TIMEOUT = 1800
HOME = Path.home()
LOGIN_DIRS = {"copilot": [".copilot"], "gemini": [".gemini"]}
INSTALL_DIRS = [".local/bin", ".local/lib"]  # where npm install --prefix ~/.local puts the CLIs


@dataclass
class Result:
    ok: bool
    text: str = ""
    tokens: int = 0
    detail: str = ""
    error: str = ""


def split_spec(spec):
    provider, _, model = spec.partition(":")
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}; choose from {', '.join(PROVIDERS)}")
    return provider, model


def _binary(provider):
    override = os.environ.get(f"AGENT_WORKFLOW_{provider.upper()}")
    return override or shutil.which(provider) or str(HOME / ".local/bin" / provider)


def _copilot_token():
    for var in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        proc = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def check(spec):
    """Why this provider can't run right now, or "" when it can. Called before a run starts."""
    provider, _ = split_spec(spec)
    if not shutil.which("bwrap"):
        return "bubblewrap (bwrap) is required to run external agents safely"
    binary = _binary(provider)
    if not (Path(binary).exists() or shutil.which(binary)):
        return f"{provider} CLI not found (tried {binary})"
    if provider == "copilot" and not _copilot_token():
        return "no GitHub token: run gh auth login, or set COPILOT_GITHUB_TOKEN"
    # Login with Google no longer works: Google rejects the CLI for consumer accounts.
    if provider == "gemini" and not os.environ.get("GEMINI_API_KEY"):
        return "gemini needs GEMINI_API_KEY, a key from https://aistudio.google.com/apikey"
    return ""


def _sandbox(argv, cwd, write, provider, env):
    cwd = str(Path(cwd).resolve())
    out = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    if Path("/run/user").exists():
        out += ["--tmpfs", "/run/user"]
    out += ["--tmpfs", str(HOME)]
    for rel in INSTALL_DIRS:
        if (HOME / rel).exists():
            out += ["--ro-bind", str(HOME / rel), str(HOME / rel)]
    for rel in LOGIN_DIRS[provider]:
        if (HOME / rel).exists():
            out += ["--bind", str(HOME / rel), str(HOME / rel)]
    out += ["--bind" if write else "--ro-bind", cwd, cwd, "--chdir", cwd,
            "--unshare-pid", "--die-with-parent", "--clearenv"]
    for key, value in env.items():
        out += ["--setenv", key, value]
    return out + argv


def _copilot_argv(binary, prompt, write, model):
    argv = [binary, "-p", prompt, "--output-format", "json", "--no-ask-user", "--allow-all-tools"]
    if not write:
        argv.append("--deny-tool=write")
    return argv + (["--model", model] if model else [])


def _gemini_argv(binary, prompt, write, model):
    argv = [binary, "-p", prompt, "--output-format", "json", "--skip-trust",
            "--approval-mode", "yolo" if write else "plan"]
    return argv + (["--model", model] if model else [])


def _parse_copilot(stdout):
    """JSON lines: the answer is the last assistant message, usage is in the checkpoint."""
    text, tokens, detail, exit_code = "", 0, "", 0
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        data = event.get("data") or {}
        if event.get("type") == "assistant.message" and data.get("content"):
            text = data["content"]
        elif event.get("type") == "session.usage_checkpoint":
            tokens = sum((m["metrics"]["usage"].get("inputTokens", 0)
                          + m["metrics"]["usage"].get("outputTokens", 0))
                         for m in data.get("providerModelMetrics") or [])
        elif event.get("type") == "result":
            exit_code = event.get("exitCode", 0)
            requests = (event.get("usage") or {}).get("premiumRequests")
            detail = f"{requests} premium requests" if requests is not None else ""
    if exit_code or not text:
        return Result(False, text, tokens, detail, f"copilot exit code {exit_code}, "
                      + ("no answer" if not text else "failed"))
    return Result(True, text, tokens, detail)


def _parse_gemini(stdout):
    """One JSON object: response text plus per-model token stats (maybe after warnings)."""
    start = stdout.find("{")
    try:
        data, _ = json.JSONDecoder().raw_decode(stdout[start:]) if start >= 0 else (None, 0)
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        return Result(False, error="gemini printed no JSON")
    models = ((data.get("stats") or {}).get("models") or {}).values()
    tokens = sum((m.get("tokens") or {}).get("total", 0) for m in models)
    if data.get("error"):
        return Result(False, tokens=tokens, error=f"gemini: {data['error'].get('message', data['error'])}")
    text = data.get("response") or ""
    return Result(bool(text), text, tokens, error="" if text else "gemini gave no answer")


ADAPTERS = {"copilot": (_copilot_argv, _parse_copilot), "gemini": (_gemini_argv, _parse_gemini)}


def run(spec, prompt, cwd, write=False, timeout=TIMEOUT):
    """Run one headless agent turn in cwd. Never raises for agent failures: the driver
    falls back to a Claude agent when ok is False."""
    provider, model = split_spec(spec)
    problem = check(spec)
    if problem:
        return Result(False, error=problem)
    build, parse = ADAPTERS[provider]
    env = {"PATH": f"{HOME}/.local/bin:{os.environ.get('PATH', '/usr/bin:/bin')}", "HOME": str(HOME),
           "LANG": os.environ.get("LANG", "C.UTF-8"), "TERM": "dumb", "NO_COLOR": "1"}
    if provider == "copilot":
        env["COPILOT_GITHUB_TOKEN"] = _copilot_token()
    if provider == "gemini":
        env["GEMINI_API_KEY"] = os.environ["GEMINI_API_KEY"]
    argv = _sandbox(build(_binary(provider), prompt, write, model), cwd, write, provider, env)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return Result(False, error=f"{provider} timed out after {timeout}s")
    result = parse(proc.stdout)
    if not result.ok and not result.error:
        result.error = proc.stderr[-300:]
    return result


def parse_findings(text):
    """Findings as JSON lines, or the line NO FINDINGS. Returns a list (maybe empty), or
    None when the reply is neither, so the caller treats it as a failed run."""
    items = []
    for line in text.replace("```json", "").replace("```", "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                items.append(item)
    if items:
        return items
    return [] if "NO FINDINGS" in text.upper() else None
