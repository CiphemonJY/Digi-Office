# Digi-Office — Full Setup Guide

This guide walks through deploying every component of the Digi-Office coordinator fleet: the central coordinator on Hermes, the Ciphemon agent on Mac, and adding any new machine as an agent.

---

## Architecture overview

```
┌──────────────────────────────────────┐
│  Coordinator  (Hermes · WSL · :8080) │
│  FastAPI · SQLite · Worker Proxy     │
└──────────┬───────────────────────────┘
           │  Tailscale mesh (all trusted)
     ┌─────┼──────────┬────────────────┐
     ▼     ▼          ▼                ▼
 Ciphemon  Hermes   Jetson           DGX
 (Mac)     (proxy)  (SSH target)     (SSH target)
 Agent SDK          No agent needed  No agent needed
```

- **Coordinator** runs on Hermes (WSL). Manages the task queue, agent registry, A2A messages, and the web dashboard.
- **Smart agents** (Ciphemon, any new machine) run the Python Agent SDK, register with the coordinator, and poll for tasks.
- **Dumb workers** (Jetson, DGX) need no new software — Hermes SSHs to them as a proxy.

---

## Prerequisites

All machines must be on the same **Tailscale tailnet** before starting.

| Requirement | Check |
|---|---|
| Python 3.10+ | `python3 --version` |
| Tailscale | `tailscale status` |
| SSH key auth Hermes→Jetson | `ssh jetson@jetson.local echo ok` |
| SSH key auth Hermes→DGX | `ssh worker@dgx-primary.local echo ok` |

---

## Step 1 — Hermes (WSL): run the coordinator

### 1a. Get the files

```bash
# Option A: copy from your Windows machine
cp -r /path/to/digi_office ~/digi_office

# Option B: clone directly if you push it to GitHub
git clone https://github.com/CiphemonJY/LISA_FTM.git
cp -r LISA_FTM/digi_office ~/digi_office
```

### 1b. Install Python dependencies

```bash
cd ~/digi_office
pip install fastapi "uvicorn[standard]" pydantic requests
```

### 1c. Test the coordinator manually

```bash
cd ~/digi_office
python -m uvicorn coordinator.server:app --host 0.0.0.0 --port 8080
```

Open `http://coordinator.local:8080/health` in your browser.
You should see: `{"status":"ok","agents_online":0,"queue_depth":0}`

Open `http://coordinator.local:8080/dashboard` to see the live dashboard.

Press `Ctrl+C` to stop, then continue to the systemd setup.

### 1d. Install as a systemd user service (auto-start)

```bash
# Copy the service file
mkdir -p ~/.config/systemd/user
cp ~/digi_office/deploy/digi-office.service ~/.config/systemd/user/

# Enable lingering so the service starts without a login session
loginctl enable-linger $USER

# Enable and start
systemctl --user daemon-reload
systemctl --user enable digi-office
systemctl --user start digi-office

# Verify
systemctl --user status digi-office
curl http://localhost:8080/health
```

### 1e. Check logs

```bash
journalctl --user -u digi-office -f
```

### Hermes verification checklist

- [ ] `curl http://coordinator.local:8080/health` returns `{"status":"ok",...}`
- [ ] Dashboard loads at `http://coordinator.local:8080/dashboard`
- [ ] Service survives a reboot: `sudo reboot` then re-check health endpoint

---

## Step 2 — Ciphemon (Mac): run the agent

### 2a. Copy the SDK and agent script

```bash
# From your Mac terminal
WORKSPACE=~/.openclaw/workspace/digi_office
SCRIPTS=~/.openclaw/scripts
LOGS=~/.openclaw/logs

mkdir -p $WORKSPACE $SCRIPTS $LOGS

# Copy from the repo (adjust path as needed)
cp -r /path/to/digi_office/agent_sdk/ $WORKSPACE/agent_sdk/
cp /path/to/digi_office/deploy/ciphemon_agent.py $SCRIPTS/
```

### 2b. Install dependencies

```bash
pip3 install requests
```

### 2c. Test the agent manually

```bash
cd ~/.openclaw/workspace/digi_office
python3 ~/.openclaw/scripts/ciphemon_agent.py
```

You should see:
```
Agent ciphemon registered. Polling every 5s
```

Open the dashboard — `ciphemon` should appear as **Online** with a green dot.

Press `Ctrl+C` to stop, then continue to the launchd setup.

### 2d. Install as a launchd service (auto-start on login)

```bash
# Copy the plist
cp /path/to/digi_office/deploy/ai.openclaw.digi-office.plist \
   ~/Library/LaunchAgents/

# Load it
launchctl load ~/Library/LaunchAgents/ai.openclaw.digi-office.plist

# Verify it's running
launchctl list | grep digi-office
tail -f ~/.openclaw/logs/digi-office.log
```

### 2e. Writing task handlers

Open `~/.openclaw/scripts/ciphemon_agent.py` and add handlers for your task types:

```python
from agent_sdk import Agent, Task

agent = Agent(
    agent_id="ciphemon",
    coordinator_url="http://coordinator.local:8080",
    capabilities=["python", "embeddings", "crosswalk", "macos"],
)

@agent.task_handler("expand_ontology")
def handle_expand(task: Task) -> dict:
    system = task.payload.get("system", "loinc")

    # Log progress visible in the dashboard feed
    agent.task_progress(task.id, f"Loading {system} data...")

    # Log a tool call — shows in feed as ⚙ event
    with agent.tool_call(task.id, "load_data", {"system": system}) as tc:
        data = load_your_data(system)
        tc["output"] = {"rows": len(data)}

    with agent.tool_call(task.id, "embed", {"n": len(data)}) as tc:
        embeddings = compute_embeddings(data)
        tc["output"] = {"shape": list(embeddings.shape)}

    # Send A2A status update to hermes
    agent.send_message("hermes", "status_update", {
        "task_id": task.id,
        "status": "done",
        "artifact": f"db_523/{system}.pkl"
    })

    return {"artifact": f"db_523/{system}.pkl"}

@agent.message_handler("status_query")
def handle_query(msg):
    agent.send_message(msg.from_agent, "status_reply", {
        "status": "ready",
        "current_task": agent._current_task_id
    })

if __name__ == "__main__":
    agent.run(poll_interval=5)
```

### Ciphemon verification checklist

- [ ] Agent appears Online in dashboard
- [ ] Submit a `data_sync` task via dashboard — Ciphemon claims it
- [ ] Task completes and shows ✓ in feed

---

## Step 3 — Adding any new agent

Any machine on the Tailnet with Python 3.10+ can become an agent in under 5 minutes.

### 3a. Copy the SDK

```bash
scp -r admin@coordinator.local:~/digi_office/agent_sdk/ ~/digi_office/agent_sdk/
pip install requests
```

Or manually copy the `agent_sdk/` folder and install `requests`.

### 3b. Create an agent script

```python
# ~/digi_office/my_agent.py
from agent_sdk import Agent, Task

agent = Agent(
    agent_id="my_new_machine",          # appears in the dashboard
    coordinator_url="http://coordinator.local:8080",
    capabilities=["python", "gpu"],     # what tasks this machine can claim
)

@agent.task_handler("llm_finetune")
def handle_finetune(task: Task) -> dict:
    model = task.payload.get("model", "llama")
    # ... your fine-tuning code ...
    return {"checkpoint": f"checkpoints/{model}_latest.pt"}

if __name__ == "__main__":
    agent.run()
```

### 3c. Run it

```bash
python3 ~/digi_office/my_agent.py
```

The agent self-registers on first heartbeat. It appears in the dashboard within 30 seconds and starts claiming tasks that match its `capabilities`.

### 3d. Register the agent's sprite (optional)

Edit `digi_office/static/sprites/sprites.json`:

```json
"agentMap": {
  "my_new_machine": "worker"
}
```

Or create a custom sprite sheet for it — see `docs/SPRITES.md`.

### 3e. Add task types to the routing table (optional)

Edit `digi_office/coordinator/routing.py` to prefer the new machine for certain task types:

```python
ROUTING_TABLE = {
    ...
    "my_task_type": {
        "default": "my_new_machine",
        "fallback": "hermes",
        "required_capabilities": ["python", "gpu"],
        "proxy": False,
    },
}
```

---

## Step 4 — Jetson / DGX (no changes needed)

These machines are **SSH proxy targets** — Hermes runs commands on them without installing any new software.

The proxy in `coordinator/worker_proxy.py` SSHs in, runs a command, and returns the output as a task result. The default command format is:

```bash
python3 -m digi_worker run '<task_type>' '<payload_json>'
```

You can change this in `WorkerProxy._build_command()` to whatever script path you prefer:

```python
def _build_command(self, task_type: str, payload: dict) -> str:
    if task_type == "fhir_validate":
        return f"python3 ~/scripts/validate_crosswalk_v2.py '{json.dumps(payload)}'"
    if task_type == "llm_finetune":
        return f"python3 ~/training/finetune.py '{json.dumps(payload)}'"
    return f"echo 'unknown task type: {task_type}'"
```

---

## Dashboard

The dashboard is served by the coordinator at:

```
http://coordinator.local:8080/dashboard
```

It auto-refreshes every 12 seconds and connects to the SSE event stream for real-time updates. If SSE fails (e.g. behind a reverse proxy that buffers), it automatically falls back to polling every 3 seconds.

**Submitting tasks manually:**  
Click **+ Task** in the header, choose a task type, set the JSON payload, and click Submit.

**Task detail:**  
Click any row in the Task Queue to see the full payload, result, error, and event log.

---

## Adding a custom task type end-to-end

1. Add handler in your agent script (`@agent.task_handler("my_type")`)
2. Add routing entry in `coordinator/routing.py`
3. Add the type to the dropdown in `static/dashboard.html` (search for `<option value="expand_ontology">`)
4. Restart the coordinator (`systemctl --user restart digi-office`)
5. Submit a task via the dashboard

---

## Troubleshooting

| Problem | Check |
|---|---|
| Agent never appears Online | `curl http://coordinator.local:8080/agents` — is it there? Check Tailscale connectivity. |
| Tasks stuck in `pending` | No agent has matching capabilities. Check `required_capabilities` in routing.py. |
| Proxy task silently fails | Run the SSH command manually from Hermes: `ssh jetson@jetson.local 'echo ok'` |
| SSE shows POLL not SSE | Normal behind Nginx without `proxy_buffering off`. Add that header or use polling mode. |
| `ModuleNotFoundError: agent_sdk` | Set `PYTHONPATH=~/digi_office` or run from the `digi_office/` directory. |
| Port 8080 unreachable | Check Windows Firewall. In WSL: `netsh interface portproxy add v4tov4 listenport=8080 connectaddress=<WSL_IP>` |

### WSL port forwarding (Windows only)

WSL2 has its own IP. If Tailscale peers can't reach port 8080 on the Windows machine directly, add a port proxy:

```powershell
# Run in Windows PowerShell as Administrator
$wslIp = (wsl hostname -I).Trim().Split(' ')[0]
netsh interface portproxy add v4tov4 listenport=8080 listenaddress=0.0.0.0 connectport=8080 connectaddress=$wslIp
netsh advfirewall firewall add rule name="WSL Digi-Office" dir=in action=allow protocol=TCP localport=8080
```

Run this again after WSL restarts (or add it to a startup task).

---

## File locations reference

```
digi_office/
├── coordinator/
│   ├── server.py         ← FastAPI app — start with uvicorn
│   ├── db.py             ← SQLite operations
│   ├── routing.py        ← task type → agent mapping
│   └── worker_proxy.py   ← SSH execution for Jetson/DGX
├── agent_sdk/
│   └── agent.py          ← copy this to each new agent machine
├── static/
│   ├── dashboard.html    ← the web UI
│   └── sprites/
│       ├── sprites.json  ← sprite config
│       ├── *.png         ← sprite sheets (generate or replace)
│       └── generate_sheets.py
├── deploy/
│   ├── digi-office.service           ← systemd (Hermes)
│   ├── ai.openclaw.digi-office.plist ← launchd (Ciphemon)
│   ├── ciphemon_agent.py             ← ready-to-use agent script
│   ├── setup_hermes.sh
│   └── setup_ciphemon.sh
├── docs/
│   ├── SETUP.md          ← this file
│   └── SPRITES.md        ← sprite creation guide
├── tests/
│   └── test_smoke.py     ← run against live coordinator
└── requirements.txt
```
