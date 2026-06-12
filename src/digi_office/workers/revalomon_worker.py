#!/usr/bin/env python3
"""
Revalomon worker loop — background daemon that watches the coordinator
queue and executes assigned tasks automatically.

Usage:
    python -m digi_office.workers.revalomon_worker \
        --coordinator http://100.113.198.30:8080 \
        --poll-interval 10

Or run standalone (coordinator URL defaults to localhost:8080 if DIGI_OFFICE_URL env is set,
otherwise falls back to localhost:8080 for the local coordinator).
"""
import argparse
import json
import logging
import os
import subprocess
import sys
import time
from functools import partial
from pathlib import Path

# Per-machine config via env; the previous hardcoded path (and an undefined
# LISA_FTM_ROOT that raised NameError in every handler) made this worker
# fail all tasks on any host but the original author's.
VENV_PYTHON = os.environ.get("LISA_VENV_PYTHON", sys.executable)
LISA_FTM_ROOT = Path(os.environ.get("LISA_FTM_ROOT", "~/LISA_FTM")).expanduser()

# ── agent_memory (optional — no hard dependency) ──────────────────────
MEMORY_DB = str(LISA_FTM_ROOT / "agent_memory.db")
try:
    import agent_memory as am
    _con = am.init_db(MEMORY_DB)
    am.recover(_con)
    logger_mem = logging.getLogger("revalomon_worker")
    logger_mem.info("agent_memory ready — %s", am.db_stats(_con))
    HAS_MEMORY = True
except ImportError:
    _con = None
    HAS_MEMORY = False


from digi_office.agent_sdk.agent import Agent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("revalomon_worker")


# ── Task handlers ────────────────────────────────────────────────────────────────

def _subprocess_result(result: subprocess.CompletedProcess) -> dict:
    """
    Normalize a subprocess outcome. A non-zero exit must FAIL the task —
    previously the stdout/stderr dict was returned regardless, so crashed
    eval scripts were recorded as status='done' (Sprint 5.5).
    Also surfaces the script's final JSON line as 'metrics' when present,
    so the coordinator stores structured results, not just log tails.
    """
    out = {
        "stdout": result.stdout[-3000:],
        "stderr": result.stderr[-2000:],
        "returncode": result.returncode,
    }
    if result.returncode != 0:
        raise RuntimeError(
            f"script exited {result.returncode}: {result.stderr[-800:] or result.stdout[-800:]}")
    for line in reversed(result.stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                out["metrics"] = json.loads(line)
            except ValueError:
                pass
            break
    return out


def handle_sprint_gate(agent: Agent, task):
    """Run perplexity gate evaluation on held-out data."""
    payload = task.payload or {}
    model = payload.get("model", "EleutherAI/pythia-160m")
    n_samples = payload.get("n_samples", 500)
    checkpoint = payload.get("checkpoint")
    dataset = payload.get("dataset", "wikitext")

    agent.task_progress(task.id, f"gate_eval", f"model={model} n_samples={n_samples} ckpt={checkpoint}")

    script = LISA_FTM_ROOT / "eval" / "gate_eval.py"
    cmd = [
        VENV_PYTHON, str(script),
        "--model", model,
        "--n-samples", str(n_samples),
    ]
    if checkpoint:
        cmd.extend(["--checkpoint", checkpoint])

    agent.task_progress(task.id, "running_gate", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    return _subprocess_result(result)


def handle_sprint_byzantine(agent: Agent, task):
    """Run Byzantine resilience stress test."""
    payload = task.payload or {}
    model = payload.get("model", "EleutherAI/pythia-70m")
    num_clients = payload.get("num_clients", 5)
    num_rounds = payload.get("num_rounds", 3)
    byzantine_method = payload.get("method", "krum")
    checkpoint = payload.get("checkpoint")
    n_malicious = payload.get("n_malicious", 1)

    agent.task_progress(task.id, "byzantine_test",
        f"model={model} clients={num_clients} method={byzantine_method}")

    script = LISA_FTM_ROOT / "eval" / "byzantine_stress_test.py"
    cmd = [
        VENV_PYTHON, str(script),
        "--model", model,
        "--clients", str(num_clients),
        "--rounds", str(num_rounds),
        "--byzantine", byzantine_method,
        "--malicious-clients", str(n_malicious),
        "--checkpoint", str(checkpoint) if checkpoint else "",
        "--tag", f"sprint5_byz_{model.split('/')[-1]}",
    ]
    if not checkpoint:
        cmd = [c for c in cmd if c != ""]

    agent.task_progress(task.id, "running_byzantine", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    return _subprocess_result(result)


def handle_sprint_heatmap(agent: Agent, task):
    """Generate LoRA layer heatmaps from a checkpoint."""
    payload = task.payload or {}
    checkpoint = payload.get("checkpoint",
        str(LISA_FTM_ROOT / "checkpoints" / "phase_c_sprint4" / "round_4.pt"))
    output_dir = payload.get("output_dir",
        str(LISA_FTM_ROOT / "reports" / "sprint5_heatmaps"))

    agent.task_progress(task.id, "heatmap_gen", f"ckpt={checkpoint}")

    script = LISA_FTM_ROOT / "scripts" / "generate_heatmaps.py"
    cmd = [VENV_PYTHON, str(script), "--checkpoint", checkpoint, "--out", output_dir]
    agent.task_progress(task.id, "running_heatmap", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return _subprocess_result(result)


def handle_generic_eval(agent: Agent, task):
    """Generic model evaluation task."""
    payload = task.payload or {}
    script = payload.get("script")
    args = payload.get("args", [])
    timeout = payload.get("timeout", 600)

    agent.task_progress(task.id, "generic_eval", f"script={script}")
    if not script:
        raise ValueError("generic_eval requires 'script' in payload")

    script_path = LISA_FTM_ROOT / "eval" / script
    if not script_path.exists():
        script_path = LISA_FTM_ROOT / script
    if not script_path.exists():
        raise FileNotFoundError(f"Script not found: {script}")

    cmd = [VENV_PYTHON, str(script_path)] + args
    agent.task_progress(task.id, "running", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return _subprocess_result(result)


# ── Message handlers ───────────────────────────────────────────────────────────

def handle_status_message(agent, message):
    """Respond to status queries from Hermesmon."""
    logger.info("Status query from %s: %s", message.from_agent, message.payload)
    agent.send_message(
        to_agent=message.from_agent,
        message_type="status_reply",
        payload={
            "agent": "revalomon",
            "status": "idle" if not agent._current_task_id else f"busy:{agent._current_task_id[:8]}",
            "capabilities": agent.capabilities,
        },
    )


def handle_action_request(agent, message):
    """Respond to action requests from Hermesmon — always reply with status."""
    logger.info("Action request from %s: %s", message.from_agent, message.payload)
    agent.send_message(
        to_agent=message.from_agent,
        message_type="action_ack",
        payload={
            "status": "received",
            "note": f"Revalomon received action_request for task {message.payload.get('task_id', 'unknown')}",
            "inbox_check_interval": "10s",
        },
    )


def handle_generic_message(agent, message):
    """Catch-all handler — logs and acknowledges any unhandled message type."""
    print(f"[catch-all] unhandled type '{message.message_type}' from {message.from_agent}: {message.payload}", file=sys.stderr)
    try:
        agent.send_message(
            to_agent=message.from_agent,
            message_type="status_reply",
            payload={
                "agent": "revalomon",
                "status": "received",
                "note": f"unhandled type '{message.message_type}' — no handler registered",
                "received_payload": message.payload,
            },
        )
        print(f"[catch-all] reply sent to {message.from_agent}", file=sys.stderr)
    except Exception as e:
        print(f"[catch-all] reply FAILED: {e}", file=sys.stderr)


def handle_command(agent, message):
    """Respond to commands from Hermesmon — execute script if present."""
    payload = message.payload
    label = payload.get("label", "unnamed")
    script = payload.get("script") or payload.get("command")
    cwd = payload.get("cwd", str(LISA_FTM_ROOT))
    logger.info("Command '%s' from %s: %s", label, message.from_agent, payload)

    if script:
        import shlex
        logger.info("Executing: %s", script)
        try:
            # Use list form to prevent shell injection
            cmd = shlex.split(script)
            result = subprocess.run(
                cmd,
                shell=False,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=120,
            )
            reply_text = f"OK rc={result.returncode}\nstdout: {result.stdout[-1000:]}\nstderr: {result.stderr[-500:]}"
        except Exception as e:
            reply_text = f"ERROR: {e}"
    else:
        reply_text = "received (no script to execute)"

    agent.send_message(
        to_agent=message.from_agent,
        message_type="command_ack",
        payload={
            "status": "done",
            "label": label,
            "result": reply_text,
        },
    )


def handle_data_sync(agent, task):
    """Handle data_sync tasks — git pull / branch sync operations."""
    payload = task.payload or {}
    action = payload.get("action", "")
    branch = payload.get("branch", "")
    cwd = str(LISA_FTM_ROOT)

    agent.task_progress(task.id, "data_sync", f"action={action} branch={branch}")

    if action == "git_pull" and branch:
        result = subprocess.run(
            ["git", "fetch", "origin", branch], cwd=cwd, capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            return {"stdout": result.stdout[-2000:], "stderr": result.stderr[-1000:], "returncode": result.returncode, "action": action, "branch": branch}
        result = subprocess.run(
            ["git", "checkout", branch], cwd=cwd, capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            return {"stdout": result.stdout[-2000:], "stderr": result.stderr[-1000:], "returncode": result.returncode, "action": action, "branch": branch}
        result = subprocess.run(
            ["git", "pull", "--ff-only", "origin", branch], cwd=cwd, capture_output=True, text=True, timeout=120
        )
        return {
            "stdout": result.stdout[-2000:],
            "stderr": result.stderr[-1000:],
            "returncode": result.returncode,
            "action": action,
            "branch": branch,
        }
    elif action == "git_push" and branch:
        result = subprocess.run(
            ["git", "push", "origin", branch], cwd=cwd, capture_output=True, text=True, timeout=60
        )
        return {
            "stdout": result.stdout[-2000:],
            "stderr": result.stderr[-1000:],
            "returncode": result.returncode,
            "action": action,
            "branch": branch,
        }
    else:
        raise ValueError(f"data_sync: unknown action '{action}' or missing branch")


# ── Main ─────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Revalomon worker daemon")
    parser.add_argument("--coordinator", default=os.environ.get("DIGI_OFFICE_URL",
                        "http://localhost:8080"))
    parser.add_argument("--poll-interval", type=int, default=10,
                        help="Seconds between polling cycles (default: 10)")
    args = parser.parse_args()

    agent = Agent(
        agent_id="revalomon",
        coordinator_url=args.coordinator,
        capabilities=["validation", "gate", "byzantine", "heatmap", "python", "git", "linux"],
    )
    if HAS_MEMORY:
        agent._memory_con = _con  # attach persistent memory connection

    # Register task handlers
    agent.task_handler("sprint4_gate")(lambda t: handle_sprint_gate(agent, t))
    agent.task_handler("sprint4_byzantine")(lambda t: handle_sprint_byzantine(agent, t))
    agent.task_handler("sprint4_heatmap")(lambda t: handle_sprint_heatmap(agent, t))
    agent.task_handler("sprint5_gate")(lambda t: handle_sprint_gate(agent, t))
    agent.task_handler("sprint5_byzantine")(lambda t: handle_sprint_byzantine(agent, t))
    agent.task_handler("byzantine_eval")(lambda t: handle_sprint_byzantine(agent, t))
    agent.task_handler("sprint5_heatmap")(lambda t: handle_sprint_heatmap(agent, t))
    agent.task_handler("model_eval")(lambda t: handle_generic_eval(agent, t))
    agent.task_handler("generic_eval")(lambda t: handle_generic_eval(agent, t))
    agent.task_handler("data_sync")(lambda t: handle_data_sync(agent, t))

    # Register message handlers
    agent.message_handler("status_query")(partial(handle_status_message, agent=agent))
    agent.message_handler("status")(partial(handle_status_message, agent=agent))
    agent.message_handler("status_update")(partial(handle_status_message, agent=agent))
    agent.message_handler("action_request")(partial(handle_action_request, agent=agent))
    agent.message_handler("command")(partial(handle_command, agent=agent))
    agent.message_handler("*")(partial(handle_generic_message, agent=agent))  # catch-all

    # Consolidate if 10+ unconsolidated events have accumulated
    if HAS_MEMORY:
        stats = am.db_stats(_con)
        if stats["events_unconsolidated"] >= 10:
            n = am.consolidate(_con)
            logger.info("consolidated %d events", n)

    logger.info("Starting Revalomon worker — coordinator=%s poll_interval=%ds",
                args.coordinator, args.poll_interval)
    agent.run(poll_interval=args.poll_interval)


if __name__ == "__main__":
    main()