import json
import os
import subprocess
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Root of the project whose scripts the proxied tasks run, on the remote node.
PROJECT_ROOT = os.environ.get("PROJECT_ROOT", "~/project")

# Fleet definition. Configure for your own machines via the DIGI_OFFICE_MACHINES
# env var (path to a JSON file shaped like _EXAMPLE_MACHINES), or via the
# per-node *_HOST / *_USER env vars below. The defaults are placeholders
# (RFC 5737 documentation IPs) — they are not real hosts.
_EXAMPLE_MACHINES = {
    "jetson": {
        "host": os.environ.get("EDGE_HOST", "203.0.113.20"),
        "user": os.environ.get("EDGE_USER", "user"),
        "key": "~/.ssh/id_ed25519",
        "ssh_binary": "ssh",
        "ssh_opts": [
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=2",
        ],
    },
    "dgx_primary": {
        "host": os.environ.get("GPU_PRIMARY_HOST", "203.0.113.10"),
        "user": os.environ.get("GPU_PRIMARY_USER", "user"),
        "ssh_binary": "ssh",
    },
    "dgx_secondary": {
        "host": os.environ.get("GPU_SECONDARY_HOST", "203.0.113.11"),
        "user": os.environ.get("GPU_SECONDARY_USER", "user"),
        "ssh_binary": "ssh",
    },
}


def _load_machines() -> dict:
    cfg = os.environ.get("DIGI_OFFICE_MACHINES")
    if cfg and os.path.exists(cfg):
        with open(cfg) as f:
            return json.load(f)
    return _EXAMPLE_MACHINES


MACHINES = _load_machines()

SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=15",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=2",  # kill after 2 missed alive messages (~60s)
]

# ── Task handler map: task_type → (script_path, venv?) ──────────────
TASK_SCRIPTS = {
    "expand_ontology": None,  # handled by Ciphemon agent, not proxy
    "ontology_validate": (PROJECT_ROOT + "/scripts/validate_crosswalk_v2.py", True),
    "ontology_quality_check": (PROJECT_ROOT + "/scripts/test_phase_b_ontology.py", True),
    "fhir_generate": None,  # placeholder until Synthea pipeline is set up
    "fhir_validate": (PROJECT_ROOT + "/scripts/validate_crosswalk_v2.py", True),
    "fhir_bundle_clean": None,  # SSH rm -rf handled inline
    "llm_finetune": None,  # placeholder
    "model_eval": (PROJECT_ROOT + "/scripts/test_phase_b_ontology.py", True),
    "model_export": None,
    "render_3d": None,
}


class WorkerProxy:
    def run(self, machine_id: str, command: str, timeout: int = 300) -> dict:
        machine = MACHINES.get(machine_id)
        if not machine:
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Unknown machine: {machine_id}",
                "duration_ms": 0,
            }

        target = f"{machine['user']}@{machine['host']}"
        key = machine.get("key")
        ssh_bin = machine.get("ssh_binary", "ssh")
        ssh_opts = machine.get("ssh_opts", SSH_OPTS)

        # Split ssh_binary for subprocess (e.g. "tailscale ssh" -> ["tailscale", "ssh"])
        ssh_argv = ssh_bin.split()

        # Wrap ssh with timeout to prevent indefinite hangs
        cmd = ["timeout", str(timeout)] + ssh_argv + ssh_opts
        if key:
            cmd += ["-i", key]
        cmd += [target, command]

        start = time.monotonic()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            return {
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "duration_ms": duration_ms,
            }
        except subprocess.TimeoutExpired:
            duration_ms = int((time.monotonic() - start) * 1000)
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"SSH timeout after {timeout}s",
                "duration_ms": duration_ms,
            }
        except Exception as e:
            duration_ms = int((time.monotonic() - start) * 1000)
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": str(e),
                "duration_ms": duration_ms,
            }

    def parse_result(self, raw: dict) -> tuple[bool, dict]:
        """Returns (success, result_payload)."""
        if raw["exit_code"] != 0:
            err = raw["stderr"] or f"exit code {raw['exit_code']}"
            # timeout command returns 124 when it kills a hung process
            if raw["exit_code"] == 124:
                err = f"SSH command timed out after {raw.get('duration_ms', 0)//1000}s"
            return False, {"error": err}

        stdout = raw["stdout"].strip()
        try:
            payload = json.loads(stdout)
            return True, payload
        except json.JSONDecodeError:
            return True, {"output": stdout, "duration_ms": raw["duration_ms"]}

    def run_task(self, machine_id: str, task_type: str,
                 payload: dict, timeout: int = 300) -> tuple[bool, dict]:
        command = self._build_command(machine_id, task_type, payload)
        raw = self.run(machine_id, command, timeout)
        return self.parse_result(raw)

    def _build_command(self, machine_id: str, task_type: str, payload: dict) -> str:
        payload_json = json.dumps(payload, separators=(',', ':')).replace("'", "\\'")

        entry = TASK_SCRIPTS.get(task_type)
        if entry is None:
            # Fallback 1: payload.commands — direct shell chain (ciphemon pattern)
            commands = payload.get("commands")
            if commands and isinstance(commands, list):
                cwd = payload.get("cwd", "$PROJECT_ROOT")
                script = " && ".join(commands)
                return f"cd {cwd} && {script}"
            # Fallback 2: generic digi_worker (placeholder)
            return f"cd $PROJECT_ROOT \u0026\u0026 python3 -m digi_worker run '{task_type}' '{payload_json}'"

        script_path, use_venv = entry

        # build python invocation
        python = PROJECT_ROOT + "/.venv/bin/python3" if use_venv else "python3"
        flags = ""
        for key, val in payload.items():
            flags += f" --{key} '{str(val).replace(chr(39), chr(92)+chr(39))}'"
        return f"cd $PROJECT_ROOT \u0026\u0026 {python} {script_path}{flags}"


proxy = WorkerProxy()
