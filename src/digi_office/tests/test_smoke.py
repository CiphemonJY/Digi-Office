"""
Smoke tests — run against a live coordinator at COORDINATOR_URL.
Usage:
  COORDINATOR_URL=http://100.119.15.111:8080 python -m pytest tests/test_smoke.py -v
"""
import json
import os
import time

import pytest
import requests

BASE = os.environ.get("COORDINATOR_URL", "http://100.119.15.111:8080")


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


# ---------- DLQ: task exhausts retries → moved to DLQ ----------
def test_dlq_moves_exhausted_task():
    r = requests.post(url("/tasks"), json={"type": "data_sync", "payload": {}, "max_retries": 2}, timeout=5)
    task_id = r.json()["id"]

    # Claim and fail twice (2 retries = max_retries)
    for _ in range(2):
        r2 = requests.get(url(f"/tasks/{task_id}"), timeout=5)
        if r2.json()["status"] in ("pending",):
            requests.get(url("/tasks/claim"), params={"agent_id": "test_agent", "capabilities": "[]"}, timeout=5)
        requests.post(url(f"/tasks/{task_id}/fail"), json={"error": "persistent failure"}, timeout=5)

    final = requests.get(url(f"/tasks/{task_id}"), timeout=5).json()
    assert final["status"] == "failed"

    # DLQ entry should exist — try both table schemas (Revalomon's `dead_letter` or Hermesmon's `dead_letter_queue`)
    for endpoint in [f"/tasks/dlq/{task_id}", f"/dlq/{task_id}"]:
        r_dlq = requests.get(url(endpoint), timeout=5)
        if r_dlq.status_code == 200:
            break
    assert r_dlq.status_code == 200, f"Neither /tasks/dlq/{task_id} nor /dlq/{task_id} returned 200"
    entry = r_dlq.json()
    assert entry["original_task_id"] == task_id or entry.get("task_id") == task_id
    assert entry["error"] == "persistent failure" or entry.get("final_error") == "persistent failure"


# ---------- DLQ: list all dead-lettered tasks ----------
def test_dlq_list():
    r = requests.get(url("/tasks/dlq"), timeout=5)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ---------- DLQ: requeue from DLQ creates new pending task ----------
def test_dlq_requeue():
    # Get an existing DLQ entry (from previous test)
    r = requests.get(url("/tasks/dlq"), timeout=5)
    dlq = r.json()
    if not dlq:
        pytest.skip("No DLQ entries available")

    original_id = dlq[0].get("original_task_id") or dlq[0].get("task_id")
    r_requeue = requests.post(url(f"/tasks/dlq/{original_id}/requeue"), json={}, timeout=5)
    assert r_requeue.status_code == 200
    result = r_requeue.json()
    assert result["ok"] is True
    assert "new_task_id" in result

    # New task should be pending
    new_task = requests.get(url(f"/tasks/{result['new_task_id']}"), timeout=5).json()
    assert new_task["status"] == "pending"


# ---------- Release: manually release a stuck task ----------
def test_release_task():
    r = requests.post(url("/tasks"), json={"type": "data_sync", "payload": {}}, timeout=5)
    task_id = r.json()["id"]

    # Claim it
    requests.get(url("/tasks/claim"), params={"agent_id": "test_agent", "capabilities": "[]"}, timeout=5)

    # Release it
    r_release = requests.post(url(f"/tasks/{task_id}/release"), timeout=5)
    assert r_release.status_code == 200
    released = r_release.json()
    assert released["status"] == "pending"
    assert released["assigned_to"] is None
