"""Stands in for `claude -p` in the driver tests: logs its argv and runs the next scripted
step for the role named in its system prompt, with a workflow store at hand."""

import json
import os
import re
import sys

from agent_workflow import gate
from agent_workflow.core import Store

args = sys.argv[1:]


def option(name):
    return args[args.index(name) + 1]


role = re.search(r"# Role: (.+)", option("--append-system-prompt")).group(1)
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
    f.write(json.dumps({"role": role, "argv": args, "cwd": os.getcwd()}) + "\n")
plan_path = os.environ["FAKE_CLAUDE_PLAN"]
with open(plan_path) as f:
    plan = json.load(f)
step = plan[role].pop(0)
with open(plan_path, "w") as f:
    json.dump(plan, f)
home = json.loads(option("--mcp-config"))["mcpServers"]["agent-workflow"]["env"]["AGENT_WORKFLOW_HOME"]
exec(step, {"store": Store(home), "gate": gate})
print(json.dumps({"type": "result", "is_error": False, "result": "ok", "usage": {
    "input_tokens": 100, "output_tokens": 50, "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 850}}))
