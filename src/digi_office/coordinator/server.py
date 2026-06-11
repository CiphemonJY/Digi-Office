import json
import logging
import asyncio
import os
import secrets
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from .db import (
    init_db, create_task, get_task, list_tasks, claim_task,
    complete_task, fail_task, upsert_agent_heartbeat, get_agents,
    get_task_log, mark_stale_agents_offline, emit, get_feed,
    send_a2a_message, get_a2a_inbox, ack_a2a_message, get_a2a_recent,
    log_tool_call, log_tool_result, list_dlq, get_dlq_entry,
    requeue_from_dlq, release_task, reclaim_stale_tasks,
    get_task_attempts, NotTaskOwner, reassign_task,
    reclaim_orphaned_proxy_tasks, cancel_task,
    create_goal, list_goals, get_goal, set_goal_status, GOAL_STATUSES,
)
from .routing import resolve_route
from .worker_proxy import proxy

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    orphaned = reclaim_orphaned_proxy_tasks()
    if orphaned:
        logger.warning("Requeued %d proxy task(s) orphaned by a coordinator restart", orphaned)
    asyncio.create_task(_stale_agent_sweeper())
    yield


app = FastAPI(title="Digi-Office Coordinator", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """
    Optional shared-secret auth: set DIGI_OFFICE_TOKEN on the coordinator and
    every agent. Mutating routes (all POSTs, plus /tasks/claim which assigns
    work) then require `Authorization: Bearer <token>`. Read-only GETs stay
    open so /health checks and the dashboard keep working. No token set =
    no auth (backward compatible).
    """
    token = os.environ.get("DIGI_OFFICE_TOKEN")
    if token and (request.method == "POST" or request.url.path == "/tasks/claim"):
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied, f"Bearer {token}"):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


async def _stale_agent_sweeper():
    while True:
        await asyncio.sleep(60)
        mark_stale_agents_offline(threshold_seconds=90)
        reclaim_stale_tasks()


# ── Models ────────────────────────────────────────────────────────────

class TaskSubmit(BaseModel):
    type: str
    payload: dict = {}
    priority: int = 1
    required_capabilities: list = []
    target_machine: Optional[str] = None
    project: str = "LISA_FTM"
    max_retries: Optional[int] = None
    goal_id: Optional[str] = None
    depends_on: list = []


class GoalSubmit(BaseModel):
    title: str
    description: str = ""
    acceptance: str = ""
    created_by: str = ""


class GoalStatusPayload(BaseModel):
    status: str
    notes: str = ""


class CancelPayload(BaseModel):
    reason: str = ""


class HeartbeatPayload(BaseModel):
    agent_id: str
    hostname: Optional[str] = None
    tailscale_ip: Optional[str] = None
    capabilities: Optional[list] = None
    current_task_id: Optional[str] = None


class CompletePayload(BaseModel):
    result_artifact: Optional[str] = None
    result_payload: Optional[dict] = None
    agent_id: Optional[str] = None


class FailPayload(BaseModel):
    error: str = ""
    agent_id: Optional[str] = None


class TaskHeartbeat(BaseModel):
    progress: Optional[str] = None
    agent_id: Optional[str] = None
    log_entry: Optional[str] = None


class A2AMessage(BaseModel):
    from_agent: str
    to_agent: Optional[str] = None
    message_type: str = "message"
    payload: dict = {}
    task_id: Optional[str] = None


class ActivityPayload(BaseModel):
    kind: str = "tool"            # e.g. 'tool:Bash', 'session_start'
    summary: str = ""             # one line, e.g. first 120 chars of a command
    detail: Optional[str] = None
    task_id: Optional[str] = None


class AckMessage(BaseModel):
    agent_id: Optional[str] = None


class ToolCallStart(BaseModel):
    agent_id: str
    tool_name: str
    tool_input: dict = {}


class ToolCallResult(BaseModel):
    agent_id: str
    tool_name: str
    tool_output: dict = {}
    duration_ms: int = 0
    success: bool = True


class RequeuePayload(BaseModel):
    max_retries: Optional[int] = None


# ── Health ─────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    agents = get_agents()
    online = sum(1 for a in agents if a["online"])
    pending = len(list_tasks(status="pending"))
    running = len(list_tasks(status="claimed")) + len(list_tasks(status="running"))
    return {"status": "ok", "agents_online": online, "queue_depth": pending + running}


# ── Tasks ──────────────────────────────────────────────────────────────

@app.post("/tasks", status_code=201)
def submit_task(body: TaskSubmit):
    if body.depends_on and not all(isinstance(d, str) and d for d in body.depends_on):
        raise HTTPException(422, "depends_on must be a list of task-id strings")
    route = resolve_route(body.type)
    caps = body.required_capabilities or route.get("required_capabilities", [])
    target = body.target_machine or (route.get("default") if route.get("proxy") else None)
    return create_task(
        type_=body.type, payload=body.payload, priority=body.priority,
        required_capabilities=caps, target_machine=target, project=body.project,
        max_retries=body.max_retries,
        goal_id=body.goal_id, depends_on=body.depends_on,
    )


@app.get("/tasks")
def list_tasks_endpoint(
    status: Optional[str] = Query(None),
    agent: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    limit: int = Query(100),
):
    return list_tasks(status=status, agent_id=agent, type_=type, limit=limit)


# ── DLQ endpoints must precede /tasks/{task_id} to avoid shadowing ─────

@app.get("/tasks/dlq")
def get_dlq(limit: int = Query(100)):
    """List all dead-lettered tasks."""
    return list_dlq(limit=limit)


@app.get("/dlq")
def get_dlq_alias(limit: int = Query(100)):
    """Backward-compatible alias for DLQ list."""
    return list_dlq(limit=limit)


@app.get("/tasks/dlq/{original_task_id}")
def get_dlq_entry_endpoint(original_task_id: str):
    """Get a single DLQ entry by original task ID."""
    entry = get_dlq_entry(original_task_id)
    if not entry:
        raise HTTPException(404, "DLQ entry not found")
    return entry


@app.post("/tasks/dlq/{original_task_id}/requeue")
def requeue_dlq(original_task_id: str, body: Optional[RequeuePayload] = None):
    """Requeue a dead-lettered task as a new pending task."""
    result = requeue_from_dlq(original_task_id,
                              max_retries=body.max_retries if body else None)
    if not result.get("ok"):
        raise HTTPException(404, result.get("error", "DLQ entry not found"))
    return result


@app.post("/dlq/{dlq_id}/recover")
def recover_dlq_endpoint(dlq_id: str):
    """Backward-compatible alias for DLQ requeue."""
    result = requeue_from_dlq(dlq_id)
    if not result.get("ok"):
        raise HTTPException(404, result.get("error", "DLQ entry not found"))
    return result


@app.get("/tasks/claim")
def claim_task_endpoint(
    agent_id: str = Query(...),
    capabilities: str = Query("[]"),
):
    caps = json.loads(capabilities)
    task = claim_task(agent_id=agent_id, capabilities=caps)
    if not task:
        return Response(status_code=204)

    route = resolve_route(task["type"])
    if route.get("proxy") and task.get("target_machine"):
        # The coordinator runs this task itself (SSH proxy thread); the agent
        # that happened to poll it is not the owner and must not be fenced
        # against or have the task reclaimed on its heartbeat.
        reassign_task(task["id"], f"proxy:{task['target_machine']}")
        _dispatch_proxy_task(task)
        return Response(status_code=204)

    return task


@app.get("/tasks/{task_id}")
def get_task_endpoint(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    task["log"] = get_task_log(task_id)
    return task


@app.get("/tasks/{task_id}/attempts")
def task_attempts_endpoint(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return {"task_id": task_id, "attempts": get_task_attempts(task_id)}


@app.post("/tasks/{task_id}/heartbeat")
def task_heartbeat(task_id: str, body: TaskHeartbeat):
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if body.log_entry:
        emit("task_progress", source=body.agent_id, task_id=task_id,
             details={"progress": body.progress, "log": body.log_entry})
    return {"ok": True}


@app.post("/tasks/{task_id}/complete")
def complete_task_endpoint(task_id: str, body: CompletePayload):
    try:
        task = complete_task(task_id, result_artifact=body.result_artifact,
                             result_payload=body.result_payload,
                             agent_id=body.agent_id)
    except NotTaskOwner as e:
        raise HTTPException(409, str(e))
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@app.post("/tasks/{task_id}/fail")
def fail_task_endpoint(task_id: str, body: FailPayload):
    try:
        task = fail_task(task_id, body.error, agent_id=body.agent_id)
    except NotTaskOwner as e:
        raise HTTPException(409, str(e))
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@app.post("/tasks/{task_id}/cancel")
def cancel_task_endpoint(task_id: str, body: Optional[CancelPayload] = None):
    """Terminal cancel (planner use): no retry, no DLQ. 409 unless the task
    is pending/claimed/running."""
    if not get_task(task_id):
        raise HTTPException(404, "Task not found")
    task = cancel_task(task_id, reason=body.reason if body else "")
    if not task:
        raise HTTPException(409, "Task is not in a cancellable state")
    return task


@app.post("/tasks/{task_id}/release")
def release_task_endpoint(task_id: str):
    """
    Release a task that an agent abandoned (went stale).
    Resets it to pending so another agent can claim it.
    """
    task = release_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found or not in claimed/running state")
    return task

@app.post("/tasks/{task_id}/tool_call")
def task_tool_call(task_id: str, body: ToolCallStart):
    log_id = log_tool_call(body.agent_id, task_id, body.tool_name, body.tool_input)
    return {"ok": True, "log_id": log_id}


@app.post("/tasks/{task_id}/tool_result")
def task_tool_result(task_id: str, body: ToolCallResult):
    log_tool_result(body.agent_id, task_id, body.tool_name,
                    body.tool_output, body.duration_ms, body.success)
    return {"ok": True}


# ── Agents ─────────────────────────────────────────────────────────────

@app.post("/agents/{agent_id}/heartbeat")
def agent_heartbeat(agent_id: str, body: HeartbeatPayload):
    upsert_agent_heartbeat(
        agent_id=agent_id, hostname=body.hostname,
        tailscale_ip=body.tailscale_ip, capabilities=body.capabilities,
        current_task_id=body.current_task_id,
    )
    return {"ok": True}


@app.get("/agents")
def list_agents():
    mark_stale_agents_offline(threshold_seconds=90)
    return get_agents()


# ── Goals (the planner's unit of intent) ───────────────────────────────

@app.post("/goals", status_code=201)
def submit_goal(body: GoalSubmit):
    return create_goal(title=body.title, description=body.description,
                       acceptance=body.acceptance, created_by=body.created_by)


@app.get("/goals")
def goals_list(status: Optional[str] = Query(None)):
    return list_goals(status=status)


@app.get("/goals/{goal_id}")
def goal_detail(goal_id: str):
    goal = get_goal(goal_id)
    if not goal:
        raise HTTPException(404, "Goal not found")
    return goal


@app.post("/goals/{goal_id}/status")
def goal_status(goal_id: str, body: GoalStatusPayload):
    if body.status not in GOAL_STATUSES:
        raise HTTPException(422, f"status must be one of {GOAL_STATUSES}")
    goal = set_goal_status(goal_id, body.status, notes=body.notes)
    if not goal:
        raise HTTPException(404, "Goal not found")
    return goal


# ── Agent activity (auto-reported by Claude Code hooks) ────────────────
# Visibility safety-net: agents' harness hooks post every tool call here, so
# the office shows real activity even when an agent works without claiming a
# task ("dark work"). Activity doubles as liveness — a busy agent never goes
# stale even if its heartbeat thread wedges.

_activity_window: dict = {}        # agent_id → [window_start_epoch, count]
_ACTIVITY_CAP = 120                # max stored events per agent per 60s


@app.post("/agents/{agent_id}/activity")
def agent_activity(agent_id: str, body: ActivityPayload):
    upsert_agent_heartbeat(agent_id=agent_id)      # liveness bump + auto-register
    now = time.monotonic()
    win = _activity_window.get(agent_id)
    if not win or now - win[0] > 60:
        _activity_window[agent_id] = win = [now, 0]
    win[1] += 1
    if win[1] > _ACTIVITY_CAP:
        # Collapse floods: keep 1-in-100 as a marker so the feed shows the
        # burst without drowning in it.
        if win[1] % 100 != 0:
            return {"ok": True, "suppressed": True}
        body.summary = f"[{win[1]} events this minute] " + (body.summary or "")
    emit("agent_activity", source=agent_id, task_id=body.task_id,
         details={"kind": body.kind, "summary": (body.summary or "")[:200],
                  "detail": (body.detail or "")[:500] or None})
    return {"ok": True}


@app.get("/agents/{agent_id}/activity")
def get_agent_activity(agent_id: str):
    """Return recent feed events for this agent and its current activity window."""
    events = get_feed(limit=80)
    agent_events = [e for e in events if e.get("source") == agent_id]
    win = _activity_window.get(agent_id)
    return {
        "agent_id": agent_id,
        "window": {"start": win[0], "count": win[1]} if win else None,
        "recent": agent_events[:20],
    }


# ── A2A ────────────────────────────────────────────────────────────────

@app.post("/a2a/messages", status_code=201)
def send_message(body: A2AMessage):
    msg_id = send_a2a_message(
        from_agent=body.from_agent, to_agent=body.to_agent,
        message_type=body.message_type, payload=body.payload,
        task_id=body.task_id,
    )
    return {"id": msg_id, "ok": True}


@app.get("/a2a/messages")
def list_messages(limit: int = Query(50)):
    return get_a2a_recent(limit=limit)


@app.get("/a2a/inbox/{agent_id}")
def get_inbox(agent_id: str, unread_only: bool = Query(True)):
    msgs = get_a2a_inbox(agent_id, unread_only=unread_only)
    # Mark DIRECTED messages delivered; a broadcast's shared status must not
    # change on first fetch or legacy status-based readers lose it.
    from .db import get_conn
    conn = get_conn()
    for m in msgs:
        if m["status"] == "sent" and m["to_agent"] is not None:
            conn.execute("UPDATE a2a_messages SET status='delivered' WHERE id=?", (m["id"],))
    conn.commit()
    conn.close()
    return msgs


@app.post("/a2a/messages/{msg_id}/ack")
def ack_message(msg_id: str, body: Optional[AckMessage] = None):
    ack_a2a_message(msg_id, agent_id=body.agent_id if body else None)
    return {"ok": True}


# ── Event feed (SSE + JSON polling) ────────────────────────────────────

@app.get("/feed")
def get_feed_json(since_id: int = Query(0), limit: int = Query(80)):
    return get_feed(since_id=since_id, limit=limit)


@app.get("/events")
async def event_stream(request: Request, since_id: int = Query(0)):
    async def generate():
        last_id = since_id
        try:
            while True:
                if await request.is_disconnected():
                    break
                events = get_feed(since_id=last_id, limit=20)
                for ev in events:
                    last_id = ev["id"]
                    yield f"id: {ev['id']}\ndata: {json.dumps(ev)}\n\n"
                if not events:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(1.5)
        except (asyncio.CancelledError, GeneratorExit):
            pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


# ── Worker proxy ───────────────────────────────────────────────────────

def _dispatch_proxy_task(task: dict):
    import threading
    threading.Thread(target=_run_proxy_task, args=(task,), daemon=True).start()


def _run_proxy_task(task: dict):
    from .db import get_conn, log_event, now_iso
    task_id = task["id"]
    machine = task.get("target_machine")
    payload = json.loads(task["payload"]) if isinstance(task["payload"], str) else task["payload"]

    conn = get_conn()
    conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
    log_event(conn, task_id, "proxy_started", details=f"machine={machine}")
    conn.commit()
    conn.close()
    emit("proxy_start", source="hermes", target=machine, task_id=task_id)

    try:
        success, result = proxy.run_task(machine, task["type"], payload)
        if success:
            complete_task(task_id, result_payload=result)
            emit("proxy_complete", source="hermes", target=machine, task_id=task_id,
                 details={"result": result})
        else:
            fail_task(task_id, result.get("error", "proxy failure"))
            emit("proxy_failed", source="hermes", target=machine, task_id=task_id,
                 details={"error": result.get("error", "proxy failure")})
    except Exception as e:
        err = str(e)[:500]
        fail_task(task_id, err)
        emit("proxy_failed", source="hermes", target=machine, task_id=task_id,
             details={"error": err, "exception": True})


# ── Dashboard ──────────────────────────────────────────────────────────

import pathlib as _pathlib
from fastapi.staticfiles import StaticFiles

_STATIC_DIR = _pathlib.Path(__file__).parent.parent / "static"

# The dashboard has always fetched /sprites/sprites.json for its PNG sheets,
# but the static dir was never mounted — sprites silently 404'd and the page
# fell back to inline pixel art forever.
app.mount("/sprites", StaticFiles(directory=str(_STATIC_DIR / "sprites")), name="sprites")
app.mount("/docs", StaticFiles(directory=str(_STATIC_DIR / "docs")), name="docs")


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse((_STATIC_DIR / "dashboard.html").read_text(encoding="utf-8"))


@app.get("/office", response_class=HTMLResponse)
def office():
    """Pixel-office view: animated agents at desks, live task flow."""
    return HTMLResponse((_STATIC_DIR / "office.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("coordinator.server:app", host="0.0.0.0", port=8080, reload=False)
