import os
"""
Agent — runs on local machine, connects to Hermes coordinator.
Deploy to: ~/.openclaw/scripts/ciphemon_agent.py
"""
import sys
import os

# Adjust path if running standalone
sys.path.insert(0, os.path.expanduser("~/.openclaw/workspace/digi_office"))

from agent_sdk import Agent, Task

COORDINATOR = os.environ.get("DIGI_OFFICE_COORDINATOR_URL", "http://localhost:8080")

agent = Agent(
    agent_id="ciphemon",
    coordinator_url=COORDINATOR,
    capabilities=["python", "embeddings", "crosswalk", "macos"],
)


@agent.task_handler("expand_ontology")
def handle_expand_ontology(task: Task) -> dict:
    system = task.payload.get("system", "loinc")
    output_path = task.payload.get("output_path", f"db_523/{system}_ontology_mem.pkl")

    # Import existing logic — adjust path as needed
    expand_script = os.path.expanduser("~/.openclaw/workspace/LISA_FTM/scripts/expand_snomed_ontology.py")
    if not os.path.exists(expand_script):
        raise FileNotFoundError(f"Expand script not found: {expand_script}")

    import subprocess
    result = subprocess.run(
        ["python3", expand_script, "--system", system, "--output", output_path],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr[:500])

    return {"artifact": output_path, "system": system}


@agent.task_handler("data_sync")
def handle_data_sync(task: Task) -> dict:
    import subprocess
    repo = task.payload.get("repo", os.path.expanduser("~/LISA_FTM"))
    result = subprocess.run(
        ["git", "-C", repo, "pull", "--rebase"],
        capture_output=True, text=True, timeout=60,
    )
    return {
        "exit_code": result.returncode,
        "output": result.stdout.strip(),
        "error": result.stderr.strip() if result.returncode != 0 else None,
    }


if __name__ == "__main__":
    agent.run(poll_interval=5)
