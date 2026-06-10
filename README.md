# Digi-Office

Agent-to-agent task coordination framework.

Used by OpenClaw (hospital CI) and LISA_FTM (federated learning) to dispatch, track, and retry tasks across autonomous AI agents (Ciphemon, Revalomon, Hermesmon).

## Quickstart

```bash
pip install digi-office
python -m digi_office.coordinator.server
```

## Components

- **Coordinator** (`digi_office.coordinator.server`): FastAPI task queue with DLQ, retries, per-agent routing
- **Agent SDK** (`digi_office.agent_sdk.agent`): Polling client for workers to claim and execute tasks
- **Workers**: Reference implementations for Revalomon, Hermesmon

## Configuration (env vars)

| Variable | Where | Effect |
|---|---|---|
| `DIGI_OFFICE_TOKEN` | coordinator + every agent | Shared-secret auth. When set on the coordinator, all POST routes and `/tasks/claim` require `Authorization: Bearer <token>`; read-only GETs (`/health`, `/dashboard`, feeds) stay open. The SDK picks it up automatically. Unset = no auth. |
| `DIGI_OFFICE_DB` | coordinator | SQLite path override (used by tests for hermetic DBs). |
| `LISA_FTM_ROOT` | workers | Path to the LISA_FTM checkout scripts run from (default `~/LISA_FTM`). |
| `LISA_VENV_PYTHON` | workers | Python interpreter for task subprocesses (default: the worker's own). |

## Coordination semantics

- **Staleness**: agents with no heartbeat for 90s go offline; their claimed/running
  tasks are failed back into the queue (retry with exponential backoff, DLQ when
  exhausted). Reclaims consume a retry so a crashing agent cannot ping-pong a task forever.
- **Fencing**: `complete`/`fail` carrying an `agent_id` are rejected with **409** if the
  task has been reassigned — a stale agent cannot overwrite the new owner's run.
- **Broadcasts** (`to_agent: null`) are tracked per recipient (`a2a_reads`); each agent
  acks independently and senders don't receive their own broadcasts. Acks must include
  `agent_id` (the SDK does this).
- **Results**: workers send `result_payload` (any JSON, `{}` included); consumers read
  the parsed `result` field on task objects.
- **Proxy tasks** (SSH-dispatched, e.g. `model_eval` → DGX) are owned by
  `proxy:<machine>`, not the agent that polled them; runs orphaned by a coordinator
  restart are requeued at boot.

## Tests

```bash
pip install -e ".[dev]"
pytest src/digi_office/tests/ -v                      # hermetic, no coordinator needed
COORDINATOR_URL=http://host:8080 pytest src/digi_office/tests/test_smoke.py -v  # live smoke
```
