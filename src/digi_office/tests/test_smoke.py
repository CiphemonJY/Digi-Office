"""
Smoke tests — run against a live coordinator at COORDINATOR_URL.
Usage:
  COORDINATOR_URL=http://localhost:8080 python -m pytest tests/test_smoke.py -v
"""
import json
import os
import time

import pytest
import requests

BASE = os.environ.get("COORDINATOR_URL", "http://localhost:8080")


def url(path):
    return f"{BASE}{path}"


# ---------- AC1: health ----------
def test_health():
    r = requests.get(url("/health"), timeout=5)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "agents_online" in body
    assert "queue_depth" in body


# ---------- AC3: submit → claim → complete ----------
def test_task_lifecycle():
    # Submit
    r = requests.post(url("/tasks"), json={"type": "data_sync", "payload": {"repo": "/tmp/test"}}, timeout=5)
    assert r.status_code == 201
    task = r.json()
    task_id = task["id"]
    assert task["status"] == "pending"

    # Claim
    r = requests.get(url("/tasks/claim"), params={"agent_id": "test_agent", "capabilities": json.dumps(["git", "ssh"])}, timeout=5)
    assert r.status_code == 200
    claimed = r.json()
    assert claimed["id"] == task_id
    assert claimed["status"] == "claimed"
    assert claimed["assigned_to"] == "test_agent"

    # Complete
    r = requests.post(url(f"/tasks/{task_id}/complete"), json={"result_payload": {"output": "Already up to date."}}, timeout=5)
    assert r.status_code == 200
    done = r.json()
    assert done["status"] == "done"


# ---------- AC4: failure → retry ----------
def test_retry_logic():
    r = requests.post(url("/tasks"), json={"type": "data_sync", "payload": {}}, timeout=5)
    task_id = r.json()["id"]

    # Claim it
    requests.get(url("/tasks/claim"), params={"agent_id": "test_agent", "capabilities": json.dumps(["git", "ssh"])}, timeout=5)

    # Fail once
    r = requests.post(url(f"/tasks/{task_id}/fail"), json={"error": "SSH timeout"}, timeout=5)
    assert r.status_code == 200
    t = r.json()
    assert t["status"] == "pending"
    assert t["retries"] == 1
    assert t["scheduled_at"] is not None


# ---------- AC5: 3 failures → status=failed ----------
def test_max_retries():
    r = requests.post(url("/tasks"), json={"type": "data_sync", "payload": {}, "max_retries": 3}, timeout=5)
    task_id = r.json()["id"]

    for _ in range(3):
        # force-reset to pending so we can claim again
        r2 = requests.get(url(f"/tasks/{task_id}"), timeout=5)
        if r2.json()["status"] in ("pending",):
            requests.get(url("/tasks/claim"), params={"agent_id": "test_agent", "capabilities": "[]"}, timeout=5)
        requests.post(url(f"/tasks/{task_id}/fail"), json={"error": "repeated failure"}, timeout=5)

    final = requests.get(url(f"/tasks/{task_id}"), timeout=5).json()
    assert final["status"] == "failed"


# ---------- AC7: dashboard returns HTML ----------
def test_dashboard():
    r = requests.get(url("/dashboard"), timeout=5)
    assert r.status_code == 200
    assert "Digi-Office" in r.text
