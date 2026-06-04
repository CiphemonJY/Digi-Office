import json
import logging
import asyncio
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from .db import (
    init_db, create_task, get_task, list_tasks, claim_task,
    complete_task, fail_task, upsert_agent_heartbeat, get_agents,
    get_task_log, mark_stale_agents_offline, emit, get_feed,
    send_a2a_message, get_a2a_inbox, ack_a2a_message, get_a2a_recent,
    log_tool_call, log_tool_result,
)
from .routing import resolve_route
from .worker_proxy import proxy

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    asyncio.create_task(_stale_agent_sweeper())
    yield


app = FastAPI(title="Digi-Office Coordinator", version="1.0.0", lifespan=lifespan)


async def _stale_agent_sweeper():
    while True:
        await asyncio.sleep(60)
        mark_stale_agents_offline(threshold_seconds=90)


# ── Models ────────────────────────────────────────────────────────────

class TaskSubmit(BaseModel):
    type: str
    payload: dict = {}
    priority: int = 1
    required_capabilities: list = []
    target_machine: Optional[str] = None
    project: str = "LISA_FTM"


class HeartbeatPayload(BaseModel):
    agent_id: str
    hostname: Optional[str] = None
    tailscale_ip: Optional[str] = None
    capabilities: Optional[list] = None
    current_task_id: Optional[str] = None


class CompletePayload(BaseModel):
    result_artifact: Optional[str] = None
    result_payload: Optional[dict] = None


class FailPayload(BaseModel):
    error: str


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


class AckMessage(BaseModel):
    pass


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
    route = resolve_route(body.type)
    caps = body.required_capabilities or route.get("required_capabilities", [])
    target = body.target_machine or (route.get("default") if route.get("proxy") else None)
    return create_task(
        type_=body.type, payload=body.payload, priority=body.priority,
        required_capabilities=caps, target_machine=target, project=body.project,
    )


@app.get("/tasks")
def list_tasks_endpoint(
    status: Optional[str] = Query(None),
    agent: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    limit: int = Query(100),
):
    return list_tasks(status=status, agent_id=agent, type_=type, limit=limit)


@app.get("/tasks/claim")
def claim_task_endpoint(
    agent_id: str = Query(...),
    capabilities: str = Query("[]"),
):
    caps = json.loads(capabilities)
    task = claim_task(agent_id=agent_id, capabilities=caps)
    if not task:
        return JSONResponse(status_code=204, content=None)

    route = resolve_route(task["type"])
    if route.get("proxy") and task.get("target_machine"):
        _dispatch_proxy_task(task)
        return JSONResponse(status_code=204, content=None)

    return task


@app.get("/tasks/{task_id}")
def get_task_endpoint(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    task["log"] = get_task_log(task_id)
    return task


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
    task = complete_task(task_id, result_artifact=body.result_artifact,
                         result_payload=body.result_payload)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@app.post("/tasks/{task_id}/fail")
def fail_task_endpoint(task_id: str, body: FailPayload):
    task = fail_task(task_id, body.error)
    if not task:
        raise HTTPException(404, "Task not found")
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
    # mark delivered
    from .db import get_conn
    conn = get_conn()
    for m in msgs:
        if m["status"] == "sent":
            conn.execute("UPDATE a2a_messages SET status='delivered' WHERE id=?", (m["id"],))
    conn.commit()
    conn.close()
    return msgs


@app.post("/a2a/messages/{msg_id}/ack")
def ack_message(msg_id: str):
    ack_a2a_message(msg_id)
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

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    import pathlib
    html_path = pathlib.Path(__file__).parent.parent / "static" / "dashboard.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("coordinator.server:app", host="0.0.0.0", port=8080, reload=False)
