#!/usr/bin/env python3
"""
Digi-Office auto-reporter — Claude Code hook.

Posts every tool call (and session start/stop) of a Claude Code agent to the
Digi-Office coordinator, so the fleet dashboard shows real activity even when
the agent forgets to claim or report a task. Reporting is moved from "something
the model must remember" to "something the harness does unconditionally".

Install (per host) — merge into the agent's Claude Code settings.json:

    "hooks": {
      "PostToolUse":  [ { "hooks": [ { "type": "command",
          "command": "python /path/to/digi_report.py" } ] } ],
      "SessionStart": [ { "hooks": [ { "type": "command",
          "command": "python /path/to/digi_report.py" } ] } ],
      "Stop":         [ { "hooks": [ { "type": "command",
          "command": "python /path/to/digi_report.py" } ] } ]
    }

Environment (set in the agent's service/profile):
    DIGI_OFFICE_URL     coordinator base URL (e.g. http://desktop:8080)  [required]
    DIGI_AGENT_ID       this agent's id (hermesmon|ciphemon|revalomon)   [required]
    DIGI_OFFICE_TOKEN   fleet token, if auth is enabled                  [optional]
    DIGI_TASK_ID        current coordinator task id, if the wrapper sets one [optional]

Design constraint: this script must NEVER break a tool call. Any failure —
coordinator down, bad JSON, missing env — exits 0 silently within ~2 seconds.
Stdlib only; no third-party imports.
"""
import json
import os
import sys
import urllib.request


def one_line(s: str, n: int = 120) -> str:
    return " ".join(str(s).split())[:n]


def summarize(event: dict) -> tuple:
    """Map a hook event to (kind, summary)."""
    name = event.get("hook_event_name") or event.get("hookEventName") or ""
    if name == "SessionStart":
        return "session_start", "agent session started"
    if name == "Stop":
        return "session_end", "agent session ended"
    # PostToolUse (default)
    tool = event.get("tool_name") or event.get("toolName") or "?"
    ti = event.get("tool_input") or event.get("toolInput") or {}
    if tool == "Bash":
        summary = one_line(ti.get("command", ""))
    elif tool in ("Edit", "Write", "Read", "NotebookEdit"):
        summary = one_line(ti.get("file_path", ""))
    elif tool in ("Glob", "Grep"):
        summary = one_line(ti.get("pattern", ""))
    else:
        summary = ""
    return f"tool:{tool}", summary


def main() -> int:
    url = os.environ.get("DIGI_OFFICE_URL", "").rstrip("/")
    agent = os.environ.get("DIGI_AGENT_ID", "")
    if not url or not agent:
        return 0
    try:
        event = json.load(sys.stdin)
    except Exception:
        event = {}
    kind, summary = summarize(event)
    payload = {"kind": kind, "summary": summary}
    task_id = os.environ.get("DIGI_TASK_ID")
    if task_id:
        payload["task_id"] = task_id

    req = urllib.request.Request(
        f"{url}/agents/{agent}/activity",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    token = os.environ.get("DIGI_OFFICE_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
