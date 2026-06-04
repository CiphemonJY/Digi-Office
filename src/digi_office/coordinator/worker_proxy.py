import json
import subprocess
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

MACHINES = {
    "jetson": {"host": "10.0.0.121", "user": "jetson"},
    "dgx_primary": {"host": "100.72.65.100", "user": "syeung"},
    "dgx_secondary": {"host": "100.99.1.84", "user": "syeung"},
}

SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=15",
]


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
        cmd = ["ssh"] + SSH_OPTS + [target, command]

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
            return False, {"error": raw["stderr"] or f"exit code {raw['exit_code']}"}

        stdout = raw["stdout"].strip()
        try:
            payload = json.loads(stdout)
            return True, payload
        except json.JSONDecodeError:
            return True, {"output": stdout, "duration_ms": raw["duration_ms"]}

    def run_task(self, machine_id: str, task_type: str,
                 payload: dict, timeout: int = 300) -> tuple[bool, dict]:
        command = self._build_command(task_type, payload)
        raw = self.run(machine_id, command, timeout)
        return self.parse_result(raw)

    def _build_command(self, task_type: str, payload: dict) -> str:
        payload_json = json.dumps(payload).replace("'", "'\\''")
        return f"python3 -m digi_worker run '{task_type}' '{payload_json}'"


proxy = WorkerProxy()
