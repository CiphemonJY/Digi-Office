"""
Ciphemon agent — runs on Mac Mini, connects to Digi-Office coordinator.
Deploy: python3 ~/LISA_FTM/digi_office/deploy/ciphemon_agent.py
"""
import sys, os, json, subprocess, numpy as np

sys.path.insert(0, os.path.expanduser("~/LISA_FTM/digi_office"))

from agent_sdk import Agent, Task

COORDINATOR = "http://100.119.15.111:8080"
TOKEN = os.environ.get(
 "DIGI_OFFICE_TOKEN",
 "8ecbedddd485c64eda2f49b7c1b78c800ddee8541eb92616a5f5a26c9ba217e1",
)

agent = Agent(
 agent_id="ciphemon",
 coordinator_url=COORDINATOR,
 capabilities=[
 "python", "embeddings", "crosswalk", "macos",
 "validation", "gate", "byzantine", "heatmap",
 "git", "linux", "ssh",
 ],
 
)


def _run_commands(task: Task):
 payload = task.payload or {}
 cmds = payload.get("commands", [payload.get("command", "echo 'no command'")])
 cwd = payload.get("cwd")
 timeout = payload.get("timeout", 1200)
 results = []
 for cmd in cmds:
  r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
  results.append({
   "command": cmd, "returncode": r.returncode,
   "stdout": r.stdout[:2000], "stderr": r.stderr[:500] if r.returncode != 0 else None,
  })
  if r.returncode != 0:
   raise RuntimeError(f"Command failed: {cmd} — {r.stderr[:500]}")
 return {"results": results}


@agent.task_handler("expand_ontology")
def handle_expand_ontology(task: Task):
 system = task.payload.get("system", "loinc")
 output_path = task.payload.get("output_path", f"db_523/{system}_ontology_mem.pkl")
 expand_script = os.path.expanduser("~/LISA_FTM/scripts/expand_snomed_ontology.py")
 if not os.path.exists(expand_script):
  raise FileNotFoundError(f"Expand script not found: {expand_script}")
 result = subprocess.run(
  ["python3", expand_script, "--system", system, "--output", output_path],
  capture_output=True, text=True, timeout=600,
 )
 if result.returncode != 0:
  raise RuntimeError(result.stderr[:500])
 return {"artifact": output_path, "system": system}


@agent.task_handler("data_sync")
def handle_data_sync(task: Task):
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


@agent.task_handler("command")
def handle_command(task: Task):
 return _run_commands(task)


@agent.task_handler("shell")
def handle_shell(task: Task):
 return _run_commands(task)


@agent.task_handler("train")
def handle_train(task: Task):
 return {
  "error": "ciphemon agent has no local GPU. Use dgx_training task type.",
  "agent": "ciphemon",
  "capabilities": agent.capabilities,
 }


@agent.task_handler("sleep_consolidation")
def handle_sleep_consolidation(task: Task):
 sys.path.insert(0, os.path.expanduser("~/LISA_FTM"))
 concept_code = task.payload.get("concept_code")
 n_passes = task.payload.get("n_passes", 4)
 if not concept_code:
  raise ValueError("Missing concept_code in payload")
 from sentence_transformers import SentenceTransformer
 model = SentenceTransformer("all-MiniLM-L6-v2")
 class RealEmbedder:
  def embed(self, text):
   return model.encode(text)
 from prototype_sleep_embedder import SleepEmbedder
 sleep = SleepEmbedder(RealEmbedder(), sleep_passes=n_passes)
 result_emb = sleep.embed(concept_code)
 return {
  "status": "success",
  "concept_code": concept_code,
  "n_passes": n_passes,
  "embedding_norm": float(np.linalg.norm(result_emb)),
  "embedding_dim": int(result_emb.shape[0]),
 }


@agent.message_handler("*")
def handle_all_messages(msg):
 from_agent = getattr(msg, "from_agent", "unknown")
 agent.send_message(from_agent, "status_reply", {
  "from": "ciphemon",
  "received_type": msg.message_type,
  "note": "Ciphemon online. sleep_consolidation handler active.",
 })


if __name__ == "__main__":
 agent.run(poll_interval=5)
