"""
Hermes agent — runs on WSL, connects to coordinator on localhost:8080.
Deploy to: ~/.config/digi-office/hermes_agent.py
"""
import sys
import os
import subprocess
import time
import requests
import json

COORDINATOR = os.environ.get("DIGI_COORDINATOR", "http://127.0.0.1:8080")
AGENT_ID = "hermes"
CAPABILITIES = ["ssh", "coordination", "linux", "wsl", "python", "git"]

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))


def heartbeat():
    try:
        r = requests.post(
            f"{COORDINATOR}/agents/{AGENT_ID}/heartbeat",
            json={
                "agent_id": AGENT_ID,
                "hostname": "hermes-wsl",
                "tailscale_ip": "100.113.198.30",
                "capabilities": CAPABILITIES,
                "current_task_id": None,
            },
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"[hermes] heartbeat failed: {e}")
        return False


def claim_task():
    try:
        r = requests.get(
            f"{COORDINATOR}/tasks/claim",
            params={"agent_id": AGENT_ID, "capabilities": json.dumps(CAPABILITIES)},
            timeout=10,
        )
        if r.status_code == 204:
            return None
        return r.json()
    except Exception as e:
        print(f"[hermes] claim failed: {e}")
        return None


def complete_task(task_id, result):
    try:
        r = requests.post(
            f"{COORDINATOR}/tasks/{task_id}/complete",
            json={"result_payload": result},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"[hermes] complete failed: {e}")
        return False


def fail_task(task_id, error):
    try:
        r = requests.post(
            f"{COORDINATOR}/tasks/{task_id}/fail",
            json={"error": str(error)[:500]},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"[hermes] fail failed: {e}")
        return False


# ── Task Handlers ────────────────────────────────────────────────────

def handle_fleet_sync(task):
    payload = task.get("payload", {})
    target = payload.get("target_machine")
    command = payload.get("command", ["whoami"])

    if target:
        # Proxy via SSH
        ssh_cmd = [
            "ssh", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
            f"syeung@{target}", *command,
        ]
    else:
        # Local execution
        ssh_cmd = command

    result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=300)
    return {
        "exit_code": result.returncode,
        "stdout": result.stdout.strip()[:2000],
        "stderr": result.stderr.strip()[:500],
        "target": target or "local",
    }


def handle_data_sync(task):
    repo = task.get("payload", {}).get("repo", os.path.expanduser("~/LISA_FTM"))
    result = subprocess.run(
        ["git", "-C", repo, "pull", "--ff-only"],
        capture_output=True, text=True, timeout=120,
    )
    return {
        "exit_code": result.returncode,
        "output": result.stdout.strip(),
        "error": result.stderr.strip() if result.returncode != 0 else None,
    }


TASK_HANDLERS = {
    "fleet_sync": handle_fleet_sync,
    "data_sync": handle_data_sync,
}


def run():
    print(f"[hermes] Agent starting — coordinator: {COORDINATOR}")
    print(f"[hermes] Capabilities: {', '.join(CAPABILITIES)}")

    current_task = None

    while True:
        # Send heartbeat
        heartbeat()

        if current_task is None:
            task = claim_task()
            if task:
                current_task = task
                task_id = task["id"]
                task_type = task["type"]
                print(f"[hermes] Claimed task {task_id}: {task_type}")

                handler = TASK_HANDLERS.get(task_type)
                if handler:
                    try:
                        result = handler(task)
                        complete_task(task_id, result)
                        print(f"[hermes] Task {task_id} completed")
                    except Exception as e:
                        fail_task(task_id, str(e))
                        print(f"[hermes] Task {task_id} failed: {e}")
                else:
                    fail_task(task_id, f"No handler for task type: {task_type}")
                    print(f"[hermes] No handler for {task_type}")

                current_task = None

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
