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
from pathlib import Path

VENV_PYTHON = "/home/jy/lisa_coord_venv/bin/python"


from digi_office.agent_sdk.agent import Agent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("revalomon_worker")


# ── Task handlers ────────────────────────────────────────────────────────────────

def handle_sprint_gate(agent: Agent, task):
    """Run perplexity gate evaluation on held-out data."""
    payload = task.payload or {}
    model = payload.get("model", "EleutherAI/pythia-160m")
    n_samples = payload.get("n_samples", 500)
    checkpoint = payload.get("checkpoint")
    dataset = payload.get("dataset", "wikitext")

    agent.task_progress(task.id, f"gate_eval", f"model={model} n_samples={n_samples} ckpt={checkpoint}")

    cmd = [
        VENV_PYTHON, "-m", "uvicorn",
        "digi_office.coordinator.server:app",
        "--host", "0.0.0.0", "--port", "8080",
    ]

    # For now, run the gate eval inline via a subprocess
    # Gate eval uses fedavg_vs_lisafedavg.py or a dedicated eval script
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
    return {
        "stdout": result.stdout[-2000:],
        "stderr": result.stderr[-2000:],
        "returncode": result.returncode,
    }


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
    return {
        "stdout": result.stdout[-2000:],
        "stderr": result.stderr[-2000:],
        "returncode": result.returncode,
    }


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
    return {
        "stdout": result.stdout[-2000:],
        "stderr": result.stderr[-2000:],
        "returncode": result.returncode,
    }


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
    return {
        "stdout": result.stdout[-3000:],
        "stderr": result.stderr[-2000:],
        "returncode": result.returncode,
    }


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
        capabilities=["validation", "gate", "byzantine", "heatmap", "python"],
    )

    # Register task handlers
    agent.task_handler("sprint4_gate")(lambda t: handle_sprint_gate(agent, t))
    agent.task_handler("sprint4_byzantine")(lambda t: handle_sprint_byzantine(agent, t))
    agent.task_handler("sprint4_heatmap")(lambda t: handle_sprint_heatmap(agent, t))
    agent.task_handler("sprint5_gate")(lambda t: handle_sprint_gate(agent, t))
    agent.task_handler("sprint5_byzantine")(lambda t: handle_sprint_byzantine(agent, t))
    agent.task_handler("sprint5_heatmap")(lambda t: handle_sprint_heatmap(agent, t))
    agent.task_handler("model_eval")(lambda t: handle_generic_eval(agent, t))
    agent.task_handler("generic_eval")(lambda t: handle_generic_eval(agent, t))

    # Register message handlers
    agent.message_handler("status_query")(handle_status_message)

    logger.info("Starting Revalomon worker — coordinator=%s poll_interval=%ds",
                args.coordinator, args.poll_interval)
    agent.run(poll_interval=args.poll_interval)


if __name__ == "__main__":
    main()