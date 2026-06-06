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
