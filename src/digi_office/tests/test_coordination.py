"""
Self-contained coordination regression tests — no live coordinator needed.
Each test targets a specific A2A/lifecycle bug fixed in the a2a-coordination
PR; the test names say which.

Run: pytest src/digi_office/tests/test_coordination.py -v
"""
import json
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

# Point the coordinator at a throwaway DB BEFORE importing it.
_tmpdir = tempfile.mkdtemp(prefix="digioffice_test_")
os.environ["DIGI_OFFICE_DB"] = os.path.join(_tmpdir, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from digi_office.coordinator import db  # noqa: E402
from digi_office.coordinator.server import app  # noqa: E402

db.init_db()
client = TestClient(app)


def iso_ago(seconds: int) -> str:
    """A timestamp `seconds` in the past, in the same ISO-'T' format agents write."""
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def submit(cap: str, **kw) -> dict:
    body = {"type": f"t_{uuid.uuid4().hex[:8]}", "payload": {},
            "required_capabilities": [cap], **kw}
    r = client.post("/tasks", json=body)
    assert r.status_code == 201
    return r.json()


def claim(agent_id: str, cap: str) -> dict:
    r = client.get("/tasks/claim", params={
        "agent_id": agent_id, "capabilities": json.dumps([cap])})
    assert r.status_code == 200, f"expected a claimable task, got {r.status_code}"
    return r.json()


# ── Bug 1: ISO-'T' vs SQLite datetime() string comparison ──────────────────
# Heartbeats are stored as "...T12:00:00Z" but were compared lexicographically
# against datetime('now') = "... 12:00:00". 'T' > ' ', so same-day agents
# could NEVER go stale; staleness only triggered at UTC date rollover.

def test_stale_agent_detected_same_day():
    agent_id = f"stale_{uuid.uuid4().hex[:6]}"
    client.post(f"/agents/{agent_id}/heartbeat", json={"agent_id": agent_id})

    conn = db.get_conn()
    conn.execute("UPDATE agents SET last_heartbeat=? WHERE id=?",
                 (iso_ago(600), agent_id))
    conn.commit()
    conn.close()

    db.mark_stale_agents_offline(threshold_seconds=90)
    agent = next(a for a in db.get_agents() if a["id"] == agent_id)
    assert agent["online"] == 0, (
        "agent heartbeating 10 minutes ago must be offline at a 90s threshold "
        "(ISO-'T' timestamps never compared stale within the same UTC day)")


def test_fresh_agent_not_marked_stale():
    agent_id = f"fresh_{uuid.uuid4().hex[:6]}"
    client.post(f"/agents/{agent_id}/heartbeat", json={"agent_id": agent_id})
    db.mark_stale_agents_offline(threshold_seconds=90)
    agent = next(a for a in db.get_agents() if a["id"] == agent_id)
    assert agent["online"] == 1


# ── Bug 2: tasks claimed by unregistered agents rotted forever ─────────────
# LEFT JOIN yields a.id IS NULL for an agent that claimed without ever
# heartbeating; `NULL OR NULL` filtered the row out of reclamation
# (coordinator audit 2026-06-06).

def test_reclaim_task_from_unregistered_agent():
    cap = f"cap_{uuid.uuid4().hex[:6]}"
    task = submit(cap)
    claim("ghost_agent_never_heartbeats", cap)

    conn = db.get_conn()
    conn.execute("UPDATE tasks SET claimed_at=? WHERE id=?",
                 (iso_ago(600), task["id"]))
    conn.commit()
    conn.close()

    reclaimed = db.reclaim_stale_tasks(threshold_seconds=90)
    assert reclaimed >= 1
    t = db.get_task(task["id"])
    assert t["status"] == "pending", "task of an unregistered agent must be reclaimed"
    assert t["retries"] == 1, "a reclaim must consume a retry (no infinite ping-pong)"
    assert t["assigned_to"] is None


def test_reclaim_exhausted_goes_to_dlq():
    cap = f"cap_{uuid.uuid4().hex[:6]}"
    task = submit(cap, max_retries=1)
    claim("ghost_agent_2", cap)

    conn = db.get_conn()
    conn.execute("UPDATE tasks SET claimed_at=? WHERE id=?",
                 (iso_ago(600), task["id"]))
    conn.commit()
    conn.close()

    db.reclaim_stale_tasks(threshold_seconds=90)
    t = db.get_task(task["id"])
    assert t["status"] == "failed"
    assert db.get_dlq_entry(task["id"]) is not None, "exhausted reclaim must dead-letter"


def test_freshly_claimed_task_not_reclaimed():
    """Grace period: claimed seconds ago by a not-yet-heartbeating agent."""
    cap = f"cap_{uuid.uuid4().hex[:6]}"
    task = submit(cap)
    claim("ghost_agent_3", cap)
    db.reclaim_stale_tasks(threshold_seconds=90)
    assert db.get_task(task["id"])["status"] == "claimed"


# ── Bug 3: broadcasts reached exactly one agent ────────────────────────────
# Read state lived in a single status column on the shared row, so the first
# ack hid a broadcast from the whole fleet. Also: senders received their own
# broadcasts back.

def test_broadcast_visible_to_all_agents_individually():
    sender, a, b = "hermes_t3", "agent_a_t3", "agent_b_t3"
    r = client.post("/a2a/messages", json={
        "from_agent": sender, "to_agent": None,
        "message_type": "fleet_notice", "payload": {"msg": uuid.uuid4().hex}})
    msg_id = r.json()["id"]

    ids_a = [m["id"] for m in client.get(f"/a2a/inbox/{a}").json()]
    ids_b = [m["id"] for m in client.get(f"/a2a/inbox/{b}").json()]
    assert msg_id in ids_a and msg_id in ids_b

    # sender must not receive its own broadcast
    ids_sender = [m["id"] for m in client.get(f"/a2a/inbox/{sender}").json()]
    assert msg_id not in ids_sender

    # A acks — message disappears for A only
    client.post(f"/a2a/messages/{msg_id}/ack", json={"agent_id": a})
    ids_a = [m["id"] for m in client.get(f"/a2a/inbox/{a}").json()]
    ids_b = [m["id"] for m in client.get(f"/a2a/inbox/{b}").json()]
    assert msg_id not in ids_a, "acked broadcast must leave the acker's inbox"
    assert msg_id in ids_b, "one agent's ack must NOT hide a broadcast from others"

    client.post(f"/a2a/messages/{msg_id}/ack", json={"agent_id": b})
    ids_b = [m["id"] for m in client.get(f"/a2a/inbox/{b}").json()]
    assert msg_id not in ids_b


def test_directed_message_ack_still_works():
    r = client.post("/a2a/messages", json={
        "from_agent": "x_t3", "to_agent": "y_t3",
        "message_type": "ping", "payload": {}})
    msg_id = r.json()["id"]
    assert msg_id in [m["id"] for m in client.get("/a2a/inbox/y_t3").json()]
    client.post(f"/a2a/messages/{msg_id}/ack", json={"agent_id": "y_t3"})
    assert msg_id not in [m["id"] for m in client.get("/a2a/inbox/y_t3").json()]


# ── Bug 4: the "result=None" family (Sprint 5.5) ───────────────────────────

def test_empty_dict_result_is_stored():
    """{} is falsy — the old `result_payload or None` dropped it to NULL."""
    cap = f"cap_{uuid.uuid4().hex[:6]}"
    task = submit(cap)
    claim("worker_t4", cap)
    r = client.post(f"/tasks/{task['id']}/complete",
                    json={"result_payload": {}, "agent_id": "worker_t4"})
    assert r.status_code == 200
    t = client.get(f"/tasks/{task['id']}").json()
    assert t["status"] == "done"
    assert t["result"] == {}, "empty result must round-trip as {}, not None"


def test_result_field_is_parsed_json():
    cap = f"cap_{uuid.uuid4().hex[:6]}"
    task = submit(cap)
    claim("worker_t5", cap)
    metrics = {"held_out_ppl": 18.52, "n": 500}
    client.post(f"/tasks/{task['id']}/complete",
                json={"result_payload": metrics, "agent_id": "worker_t5"})
    t = client.get(f"/tasks/{task['id']}").json()
    assert t["result"] == metrics, "consumers read task['result'], not a JSON string"


# ── Bug 5: zombie agents overwriting reassigned tasks ──────────────────────

def test_fencing_rejects_stale_owner():
    cap = f"cap_{uuid.uuid4().hex[:6]}"
    task = submit(cap)
    claim("slow_agent", cap)

    # Coordinator releases the task (slow_agent presumed dead), new agent claims.
    client.post(f"/tasks/{task['id']}/release")
    claimed = claim("new_agent", cap)
    assert claimed["id"] == task["id"]

    # The zombie finishes anyway — must be rejected, not overwrite new_agent's run.
    r = client.post(f"/tasks/{task['id']}/complete",
                    json={"result_payload": {"stale": True}, "agent_id": "slow_agent"})
    assert r.status_code == 409
    assert db.get_task(task["id"])["status"] == "claimed"

    r = client.post(f"/tasks/{task['id']}/fail",
                    json={"error": "zombie failure", "agent_id": "slow_agent"})
    assert r.status_code == 409, "a zombie's failure report must not burn the new owner's retry"

    # The rightful owner completes normally.
    r = client.post(f"/tasks/{task['id']}/complete",
                    json={"result_payload": {"ok": 1}, "agent_id": "new_agent"})
    assert r.status_code == 200
    assert db.get_task(task["id"])["status"] == "done"


def test_complete_without_agent_id_still_works():
    """Backward compatibility: older workers don't send agent_id."""
    cap = f"cap_{uuid.uuid4().hex[:6]}"
    task = submit(cap)
    claim("legacy_worker", cap)
    r = client.post(f"/tasks/{task['id']}/complete", json={"result_payload": {"v": 1}})
    assert r.status_code == 200
    assert r.json()["status"] == "done"
