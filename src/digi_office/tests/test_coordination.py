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


# ── Fix 6: optional shared-secret auth (DIGI_OFFICE_TOKEN) ─────────────────

def test_auth_enforced_when_token_set():
    os.environ["DIGI_OFFICE_TOKEN"] = "s3cret"
    try:
        body = {"type": f"t_{uuid.uuid4().hex[:8]}", "payload": {},
                "required_capabilities": ["authcap"]}
        # no token → 401
        assert client.post("/tasks", json=body).status_code == 401
        # wrong token → 401
        r = client.post("/tasks", json=body,
                        headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401
        # claim (GET, but mutating) → 401
        r = client.get("/tasks/claim", params={
            "agent_id": "x", "capabilities": "[]"})
        assert r.status_code == 401
        # right token → 201
        r = client.post("/tasks", json=body,
                        headers={"Authorization": "Bearer s3cret"})
        assert r.status_code == 201
        # read-only routes stay open (health checks, dashboard)
        assert client.get("/health").status_code == 200
    finally:
        del os.environ["DIGI_OFFICE_TOKEN"]


def test_no_auth_when_token_unset():
    assert "DIGI_OFFICE_TOKEN" not in os.environ
    body = {"type": f"t_{uuid.uuid4().hex[:8]}", "payload": {},
            "required_capabilities": ["authcap2"]}
    assert client.post("/tasks", json=body).status_code == 201


# ── Fix 7: proxy task ownership ─────────────────────────────────────────────
# The claim endpoint used to leave proxy-routed tasks assigned to whichever
# agent happened to poll them, even though a coordinator thread runs the work.
# That agent's death would reclaim a healthy proxy run, and the proxy's
# completion could be fenced. Proxy tasks now belong to 'proxy:<machine>'.

def test_proxy_claim_reassigns_to_proxy_owner(monkeypatch):
    import time as _time
    from digi_office.coordinator import server as srv

    release = {"go": False}

    def fake_run_task(machine, task_type, payload, timeout=300):
        while not release["go"]:
            _time.sleep(0.02)
        return True, {"proxied": True}

    monkeypatch.setattr(srv.proxy, "run_task", fake_run_task)

    # model_eval routes to dgx_primary with proxy=True; no capability gate.
    r = client.post("/tasks", json={"type": "model_eval", "payload": {"x": 1}})
    task_id = r.json()["id"]
    assert r.json()["target_machine"] == "dgx_primary"

    r = client.get("/tasks/claim", params={
        "agent_id": "innocent_poller", "capabilities": "[]"})
    assert r.status_code == 204, "proxy dispatch returns no task to the poller"

    t = db.get_task(task_id)
    assert t["assigned_to"] == "proxy:dgx_primary", (
        "proxy task must belong to the proxy, not the polling agent")

    # While the proxy 'runs', the sweeper must not reclaim it even with a
    # stale claimed_at and no heartbeating 'proxy:dgx_primary' agent.
    conn = db.get_conn()
    conn.execute("UPDATE tasks SET claimed_at=? WHERE id=?",
                 (iso_ago(600), task_id))
    conn.commit()
    conn.close()
    db.reclaim_stale_tasks(threshold_seconds=90)
    assert db.get_task(task_id)["status"] in ("claimed", "running")

    # Let the proxy finish and verify completion lands.
    release["go"] = True
    for _ in range(100):
        if db.get_task(task_id)["status"] == "done":
            break
        _time.sleep(0.05)
    t = db.get_task(task_id)
    assert t["status"] == "done"
    assert t["result"] == {"proxied": True}


# ── Goal layer: dependencies, cancel, rollups ───────────────────────────────

def _mk_goal(**kw):
    body = {"title": "test goal", "description": "d", "acceptance": "a",
            "created_by": "james", **kw}
    r = client.post("/goals", json=body)
    assert r.status_code == 201
    return r.json()


def test_dependency_gating_orders_pipeline():
    cap = f"cap_{uuid.uuid4().hex[:6]}"
    g = _mk_goal()
    a = submit(cap, goal_id=g["id"])
    rb = client.post("/tasks", json={"type": "stage_b", "payload": {},
                                     "required_capabilities": [cap],
                                     "goal_id": g["id"], "depends_on": [a["id"]]})
    b = rb.json()
    rc = client.post("/tasks", json={"type": "stage_c", "payload": {},
                                     "required_capabilities": [cap],
                                     "goal_id": g["id"], "depends_on": [b["id"]]})
    c = rc.json()

    # Only A is claimable: B and C are dependency-blocked.
    first = claim("pipe_agent", cap)
    assert first["id"] == a["id"]
    r = client.get("/tasks/claim", params={"agent_id": "pipe_agent",
                                           "capabilities": json.dumps([cap])})
    assert r.status_code == 204, "B must not be claimable while A is running"

    # A completes → B unblocks; C still blocked.
    client.post(f"/tasks/{a['id']}/complete",
                json={"result_payload": {"ok": 1}, "agent_id": "pipe_agent"})
    second = claim("pipe_agent", cap)
    assert second["id"] == b["id"]
    r = client.get("/tasks/claim", params={"agent_id": "pipe_agent",
                                           "capabilities": json.dumps([cap])})
    assert r.status_code == 204

    client.post(f"/tasks/{b['id']}/complete",
                json={"result_payload": {"ok": 2}, "agent_id": "pipe_agent"})
    third = claim("pipe_agent", cap)
    assert third["id"] == c["id"]


def test_dead_lettered_parent_keeps_child_blocked():
    cap = f"cap_{uuid.uuid4().hex[:6]}"
    parent = submit(cap, max_retries=1)
    client.post("/tasks", json={"type": "child", "payload": {},
                                "required_capabilities": [cap],
                                "depends_on": [parent["id"]]})
    claim("dlq_pipe_agent", cap)
    client.post(f"/tasks/{parent['id']}/fail", json={"error": "boom"})
    assert db.get_dlq_entry(parent["id"]) is not None
    r = client.get("/tasks/claim", params={"agent_id": "dlq_pipe_agent",
                                           "capabilities": json.dumps([cap])})
    assert r.status_code == 204, "child of a dead-lettered parent must stay blocked"


def test_unknown_dependency_blocks():
    cap = f"cap_{uuid.uuid4().hex[:6]}"
    client.post("/tasks", json={"type": "orphan_dep", "payload": {},
                                "required_capabilities": [cap],
                                "depends_on": ["no-such-task-id"]})
    r = client.get("/tasks/claim", params={"agent_id": "x",
                                           "capabilities": json.dumps([cap])})
    assert r.status_code == 204


def test_cancel_is_terminal_and_skips_dlq():
    cap = f"cap_{uuid.uuid4().hex[:6]}"
    t = submit(cap)
    r = client.post(f"/tasks/{t['id']}/cancel", json={"reason": "superseded"})
    assert r.status_code == 200
    got = db.get_task(t["id"])
    assert got["status"] == "failed" and got["error"].startswith("cancelled:")
    assert db.get_dlq_entry(t["id"]) is None, "cancel must not dead-letter"
    # cancelling a finished task → 409
    r = client.post(f"/tasks/{t['id']}/cancel", json={"reason": "again"})
    assert r.status_code == 409


def test_goal_rollup_and_notes_append():
    cap = f"cap_{uuid.uuid4().hex[:6]}"
    g = _mk_goal(title="rollup goal")
    t1 = submit(cap, goal_id=g["id"])
    submit(cap, goal_id=g["id"])
    claim("rollup_agent", cap)
    client.post(f"/tasks/{t1['id']}/complete",
                json={"result_payload": {"ppl": 18.5}, "agent_id": "rollup_agent"})

    detail = client.get(f"/goals/{g['id']}").json()
    ru = detail["rollup"]
    assert ru["total"] == 2 and ru["by_status"]["done"] == 1
    assert ru["finished"][0]["result"] == {"ppl": 18.5}

    client.post(f"/goals/{g['id']}/status", json={"status": "blocked", "notes": "first note"})
    client.post(f"/goals/{g['id']}/status", json={"status": "active", "notes": "second note"})
    notes = client.get(f"/goals/{g['id']}").json()["notes"]
    assert "first note" in notes and "second note" in notes, "notes must append, not replace"


def test_goal_status_validation_and_token():
    g = _mk_goal()
    r = client.post(f"/goals/{g['id']}/status", json={"status": "bogus"})
    assert r.status_code == 422
    os.environ["DIGI_OFFICE_TOKEN"] = "s3cret"
    try:
        r = client.post("/goals", json={"title": "x"})
        assert r.status_code == 401
    finally:
        del os.environ["DIGI_OFFICE_TOKEN"]


def test_legacy_task_without_goal_fields_unchanged():
    cap = f"cap_{uuid.uuid4().hex[:6]}"
    t = submit(cap)                      # no goal_id / depends_on in body
    claimed = claim("legacy_agent2", cap)
    assert claimed["id"] == t["id"]
    r = client.post(f"/tasks/{t['id']}/complete", json={"result_payload": {"v": 1}})
    assert r.json()["status"] == "done"


# ── Agent activity endpoint (auto-reporting hooks) ─────────────────────────

def test_activity_lands_in_feed_and_bumps_liveness():
    aid = f"hookagent_{uuid.uuid4().hex[:6]}"
    r = client.post(f"/agents/{aid}/activity",
                    json={"kind": "tool:Bash", "summary": "pytest -q"})
    assert r.status_code == 200
    # unknown agent auto-registered + alive
    agent = next(a for a in db.get_agents() if a["id"] == aid)
    assert agent["online"] == 1
    # event visible in the feed
    evs = client.get("/feed?since_id=0&limit=500").json()
    mine = [e for e in evs if e["event_type"] == "agent_activity" and e["source"] == aid]
    assert mine and mine[-1]["details"]["summary"] == "pytest -q"


def test_activity_flood_is_collapsed():
    aid = f"floodagent_{uuid.uuid4().hex[:6]}"
    for i in range(180):
        client.post(f"/agents/{aid}/activity", json={"kind": "tool:Bash", "summary": f"cmd {i}"})
    evs = client.get("/feed?since_id=0&limit=2000").json()
    mine = [e for e in evs if e["event_type"] == "agent_activity" and e["source"] == aid]
    # 120 stored + 1-in-100 markers beyond the cap, not 180
    assert len(mine) < 130, f"flood not collapsed: {len(mine)} events stored"


def test_activity_requires_token_when_set():
    os.environ["DIGI_OFFICE_TOKEN"] = "s3cret"
    try:
        r = client.post("/agents/x/activity", json={"kind": "t", "summary": "s"})
        assert r.status_code == 401
        r = client.post("/agents/x/activity", json={"kind": "t", "summary": "s"},
                        headers={"Authorization": "Bearer s3cret"})
        assert r.status_code == 200
    finally:
        del os.environ["DIGI_OFFICE_TOKEN"]


# ── Pixel office view + static sprite mount ────────────────────────────────

def test_office_page_served():
    r = client.get("/office")
    assert r.status_code == 200
    assert "DIGI-OFFICE" in r.text and "pixel" in r.text.lower()


def test_sprites_static_mounted():
    """The dashboard always fetched /sprites/sprites.json but the static dir
    was never mounted — PNG sprites silently 404'd forever."""
    r = client.get("/sprites/sprites.json")
    assert r.status_code == 200
    cfg = r.json()
    assert "sprites" in cfg


def test_orphaned_proxy_tasks_requeued_on_startup():
    """Coordinator died mid-proxy-run: recover at boot via retry/DLQ path."""
    cap = f"cap_{uuid.uuid4().hex[:6]}"
    task = submit(cap)
    conn = db.get_conn()
    conn.execute(
        "UPDATE tasks SET status='running', assigned_to='proxy:dgx_primary', claimed_at=? WHERE id=?",
        (iso_ago(600), task["id"]))
    conn.commit()
    conn.close()

    n = db.reclaim_orphaned_proxy_tasks()
    assert n >= 1
    t = db.get_task(task["id"])
    assert t["status"] == "pending"
    assert t["retries"] == 1
    assert t["assigned_to"] is None
