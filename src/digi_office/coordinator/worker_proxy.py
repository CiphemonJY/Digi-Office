import json
import subprocess
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

MACHINES = {
    "jetson": {
        "host": "10.0.0.121",
        "user": "jetson",
        "key": "~/.ssh/id_ed25519_openclaw",
        "ssh_binary": "ssh",
        "ssh_opts": [
            "-o", "StrictHostKeyChecking=no",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=2",
        ],
    },
    "dgx_primary": {
        "host": "100.72.65.100",
        "user": "syeung",
        "ssh_binary": "tailscale ssh",
        "ssh_opts": [],
    },
    "dgx_secondary": {
        "host": "100.99.1.84",
        "user": "syeung",
        "ssh_binary": "tailscale ssh",
        "ssh_opts": [],
    },
}

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
    "ontology_validate": ("~/LISA_FTM/scripts/validate_crosswalk_v2.py", True),
    "ontology_quality_check": ("~/LISA_FTM/scripts/test_phase_b_ontology.py", True),
    "fhir_generate": None,  # placeholder until Synthea pipeline is set up
    "fhir_validate": ("~/LISA_FTM/scripts/validate_crosswalk_v2.py", True),
    "fhir_bundle_clean": None,  # SSH rm -rf handled inline
    "llm_finetune": None,  # placeholder — GPU tasks route to DGX via TASK_GPU_MACHINES
    "model_eval": ("~/LISA_FTM/scripts/test_phase_b_ontology.py", True),
    "model_export": None,
    "render_3d": None,
}

# ── GPU task routing: task_type → machine_id ─────────────────────────
# Any task_type listed here MUST run on the specified GPU machine.
TASK_GPU_MACHINES = {
    "llm_finetune": "dgx_primary",           # Sprint 6 training
    "sprint_training": "dgx_primary",        # Phase C DP-LoRA training
    "byzantine_stress_test": "dgx_primary",  # GPU-based stress tests
    "gate_eval": "dgx_primary",              # Perplexity gating on GPU
    "model_export": "dgx_primary",           # Export checkpoints from GPU
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
        """Route GPU tasks to DGX regardless of requested machine_id."""
        # Force GPU tasks to their assigned DGX machine
        if task_type in TASK_GPU_MACHINES:
            target_machine = TASK_GPU_MACHINES[task_type]
            if machine_id != target_machine:
                logger.info(
                    "Rerouting GPU task '%s' from %s → %s",
                    task_type, machine_id, target_machine
                )
                machine_id = target_machine
        command = self._build_command(machine_id, task_type, payload)
        raw = self.run(machine_id, command, timeout)
        return self.parse_result(raw)

    def _build_command(self, machine_id: str, task_type: str, payload: dict) -> str:
        payload_json = json.dumps(payload, separators=(',', ':'))

        entry = TASK_SCRIPTS.get(task_type)
        if entry is None:
            # Fallback 1: payload.commands — direct shell chain (ciphemon pattern)
            commands = payload.get("commands")
            if commands and isinstance(commands, list):
                cwd = payload.get("cwd", "~/LISA_FTM")
                script = " && ".join(commands)
                return f"cd {cwd} && {script}"
            # Fallback 2: generic digi_worker placeholder
            return f"cd ~/LISA_FTM && python3 -m digi_worker run '{task_type}' '{payload_json}'"

        script_path, use_venv = entry
        python = "python3"
        if use_venv and MACHINES.get(machine_id):
            # Most DGX/Jetson machines have a .venv
            python = ".venv/bin/python"

        # Build args from payload
        args = []
        for key, val in payload.items():
            if val is True:
                args.append(f"--{key}")
            elif val is False or val is None:
                continue
            else:
                args.append(f"--{key} {val}")

        args_str = " ".join(args)
        return f"cd ~/LISA_FTM && {python} {script_path} {args_str}"

    @staticmethod
    def requires_gpu(task_type: str, payload: dict = None) -> bool:
        """Check if a task requires GPU based on type or payload flags."""
        if task_type in TASK_GPU_MACHINES:
            return True
        # Also detect from payload hints (e.g., device=cuda, use_gpu=true)
        if payload:
            device = str(payload.get("device", "")).lower()
            if "cuda" in device or payload.get("use_gpu") is True:
                return True
        return False

    @staticmethod
    def get_gpu_machine(task_type: str) -> Optional[str]:
        """Return the DGX machine assigned to a GPU task type, or None."""
        return TASK_GPU_MACHINES.get(task_type)


proxy = WorkerProxy()
