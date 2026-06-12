"""
Example worker agent — connects to a coordinator and handles two task types.
A reference for writing your own agent; configure via env vars.

    DIGI_OFFICE_URL   coordinator base URL (default http://127.0.0.1:8080)
    AGENT_ID          this agent's id (default "worker")
    PROJECT_ROOT      path to the project whose scripts the tasks run
"""
import os

from digi_office.agent_sdk.agent import Agent, Task

COORDINATOR = os.environ.get("DIGI_OFFICE_URL", "http://127.0.0.1:8080")
AGENT_ID = os.environ.get("AGENT_ID", "worker")
PROJECT_ROOT = os.path.expanduser(os.environ.get("PROJECT_ROOT", "~/project"))

agent = Agent(
    agent_id=AGENT_ID,
    coordinator_url=COORDINATOR,
    capabilities=["python", "embeddings"],
)


@agent.task_handler("expand_ontology")
def handle_expand_ontology(task: Task) -> dict:
    system = task.payload.get("system", "loinc")
    output_path = task.payload.get("output_path", f"db_523/{system}_ontology_mem.pkl")

    # Import existing logic — adjust path as needed
    expand_script = os.path.join(PROJECT_ROOT, "scripts", "expand_snomed_ontology.py")
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
    repo = task.payload.get("repo", PROJECT_ROOT)
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
