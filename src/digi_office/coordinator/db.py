import sqlite3
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent.parent / "data" / "digioffice.db"


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            project TEXT DEFAULT 'LISA_FTM',
            priority INTEGER DEFAULT 1,
            payload TEXT,
            required_capabilities TEXT DEFAULT '[]',
            status TEXT CHECK(status IN ('pending','claimed','running','done','failed')) DEFAULT 'pending',
            assigned_to TEXT,
            created_at TEXT,
            claimed_at TEXT,
            completed_at TEXT,
            result_artifact TEXT,
            error TEXT,
            retries INTEGER DEFAULT 0,
            max_retries INTEGER DEFAULT 3,
            scheduled_at TEXT,
            target_machine TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_status_priority
            ON tasks(status, priority, created_at);

        CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY,
            hostname TEXT,
            tailscale_ip TEXT,
            capabilities TEXT DEFAULT '[]',
            online INTEGER DEFAULT 0,
            last_heartbeat TEXT,
            current_task_id TEXT,
            registered_at TEXT
        );

        -- Unified event log: tasks, A2A, tool calls, agent events
        CREATE TABLE IF NOT EXISTS event_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            source TEXT,
            target TEXT,
            task_id TEXT,
            timestamp TEXT NOT NULL,
            details TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_event_log_ts ON event_log(timestamp DESC);

        -- A2A messages (inbox management separate from event stream)
        CREATE TABLE IF NOT EXISTS a2a_messages (
            id TEXT PRIMARY KEY,
            from_agent TEXT NOT NULL,
            to_agent TEXT,
            message_type TEXT DEFAULT 'message',
            payload TEXT,
            status TEXT CHECK(status IN ('sent','delivered','read')) DEFAULT 'sent',
            created_at TEXT,
            task_id TEXT
        );

        -- Dead letter queue: tasks that exhausted retries or unrecoverable
        CREATE TABLE IF NOT EXISTS dead_letter_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            type TEXT,
            project TEXT,
            payload TEXT,
            required_capabilities TEXT,
            final_error TEXT,
            retry_count INTEGER,
            max_retries INTEGER,
            failed_at TEXT NOT NULL,
            recovered_at TEXT,
            agent_id TEXT,
            details TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_dlq_task ON dead_letter_queue(task_id);
        CREATE INDEX IF NOT EXISTS idx_dlq_project ON dead_letter_queue(project);

        -- Attempt history: one row per task attempt (success or failure)
        CREATE TABLE IF NOT EXISTS task_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL,
            agent_id TEXT,
            status TEXT CHECK(status IN ('started','completed','failed')) NOT NULL,
            error TEXT,
            started_at TEXT,
            finished_at TEXT,
            result_artifact TEXT,
            details TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_attempts_task ON task_attempts(task_id);

        -- Backwards-compatible task_log alias
        CREATE TABLE IF NOT EXISTS task_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            event TEXT,
            agent_id TEXT,
            timestamp TEXT,
            details TEXT
        );
    """)
    conn.commit()
    conn.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Event log ─────────────────────────────────────────────────────────

def emit(event_type: str, source: str = None, target: str = None,
         task_id: str = None, details: dict = None,
         conn: sqlite3.Connection = None) -> int:
    own = conn is None
    if own:
        conn = get_conn()
    cursor = conn.execute(
        "INSERT INTO event_log (event_type, source, target, task_id, timestamp, details) VALUES (?,?,?,?,?,?)",
        (event_type, source, target, task_id, now_iso(), json.dumps(details or {})),
    )
    log_id = cursor.lastrowid
    if own:
        conn.commit()
        conn.close()
    return log_id


def get_feed(since_id: int = 0, limit: int = 80) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM event_log WHERE id > ? ORDER BY id ASC LIMIT ?",
        (since_id, limit),
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["details"] = json.loads(d["details"] or "{}")
        except Exception:
            d["details"] = {}
        result.append(d)
    return result


# ── Task operations ────────────────────────────────────────────────────

def log_event(conn: sqlite3.Connection, task_id: str, event: str,
              agent_id: Optional[str] = None, details: Optional[str] = None):
    conn.execute(
        "INSERT INTO task_log (task_id, event, agent_id, timestamp, details) VALUES (?,?,?,?,?)",
        (task_id, event, agent_id, now_iso(), details),
    )


def create_task(type_: str, payload: dict, priority: int = 1,
                required_capabilities: list = None, target_machine: str = None,
                project: str = "LISA_FTM") -> dict:
    task_id = str(uuid.uuid4())
    conn = get_conn()
    conn.execute(
        """INSERT INTO tasks
           (id, type, project, priority, payload, required_capabilities,
            status, created_at, target_machine)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (task_id, type_, project, priority,
         json.dumps(payload),
         json.dumps(required_capabilities or []),
         "pending", now_iso(), target_machine),
    )
    log_event(conn, task_id, "created")
    conn.commit()
    conn.close()
    # emit("task_created", task_id=task_id, details={"type": type_, "priority": priority})
    return get_task(task_id)


def get_task(task_id: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_tasks(status: str = None, agent_id: str = None,
               type_: str = None, limit: int = 100) -> list:
    conn = get_conn()
    clauses, params = [], []
    if status:
        clauses.append("status=?"); params.append(status)
    if agent_id:
        clauses.append("assigned_to=?"); params.append(agent_id)
    if type_:
        clauses.append("type=?"); params.append(type_)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM tasks {where} ORDER BY priority DESC, created_at DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def claim_task(agent_id: str, capabilities: list) -> Optional[dict]:
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Fetch all pending tasks ordered by priority, then find first with matching caps
        rows = conn.execute("""
            SELECT id, type, payload, required_capabilities, target_machine, retries
            FROM tasks
            WHERE status = 'pending'
              AND (scheduled_at IS NULL OR scheduled_at <= datetime('now'))
            ORDER BY priority DESC, created_at ASC
        """).fetchall()

        for row in rows:
            required = json.loads(row["required_capabilities"])
            if required and not all(cap in capabilities for cap in required):
                continue  # Skip — agent doesn't have the required capabilities

            conn.execute(
                """UPDATE tasks SET status='claimed', assigned_to=?, claimed_at=?
                   WHERE id=? AND status='pending'""",
                (agent_id, now_iso(), row["id"]),
            )
            if conn.total_changes == 0:
                continue  # Race — someone else claimed it

            log_event(conn, row["id"], "claimed", agent_id)
            # Record attempt start
            attempt_num = (row["retries"] or 0) + 1
            conn.execute(
                """INSERT INTO task_attempts
                   (task_id, attempt_number, agent_id, status, started_at)
                   VALUES (?,?,?,?,?)""",
                (row["id"], attempt_num, agent_id, "started", now_iso()),
            )
            conn.execute("COMMIT")

            task_row = conn.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
            task = dict(task_row) if task_row else None
            return task

        conn.execute("COMMIT")
        return None
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def complete_task(task_id: str, result_artifact: str = None,
                  result_payload: dict = None) -> Optional[dict]:
    conn = get_conn()
    try:
        task_row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        task = dict(task_row) if task_row else None
        artifact = result_artifact or (json.dumps(result_payload) if result_payload else None)
        conn.execute(
            """UPDATE tasks SET status='done', completed_at=?, result_artifact=?
               WHERE id=?""",
            (now_iso(), artifact, task_id),
        )
        conn.execute("UPDATE agents SET current_task_id=NULL WHERE current_task_id=?", (task_id,))
        log_event(conn, task_id, "completed")
        # Update the active attempt
        log_attempt_completion(task_id, result_artifact=artifact)
        conn.commit()
    finally:
        conn.close()

    emit("task_done", source=task["assigned_to"] if task else None, task_id=task_id,
         details={"type": task["type"] if task else None})
    return get_task(task_id)


def fail_task(task_id: str, error: str, agent_id: Optional[str] = None) -> Optional[dict]:
    conn = get_conn()
    try:
        task_row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        task = dict(task_row) if task_row else None
        if not task:
            return None

        retries = task["retries"] + 1
        max_retries = task["max_retries"]
        attempt_num = retries  # matches retry count semantics

        # Log the failed attempt
        conn.execute(
            """INSERT INTO task_attempts
               (task_id, attempt_number, agent_id, status, error, finished_at)
               VALUES (?,?,?,?,?,?)""",
            (task_id, attempt_num, agent_id or task.get("assigned_to"), "failed",
             error, now_iso()),
        )

        if retries < max_retries:
            delay_minutes = 2 ** retries
            conn.execute(
                """UPDATE tasks SET status='pending', retries=?, error=?,
                   scheduled_at=datetime('now', ?), assigned_to=NULL
                   WHERE id=?""",
                (retries, error, f"+{delay_minutes} minutes", task_id),
            )
            log_event(conn, task_id, "retry", details=f"retry {retries}/{max_retries}")
        else:
            conn.execute(
                """UPDATE tasks SET status='failed', retries=?, error=?, completed_at=?
                   WHERE id=?""",
                (retries, error, now_iso(), task_id),
            )
            log_event(conn, task_id, "failed", details=error)

            # Write to dead-letter queue
            conn.execute(
                """INSERT INTO dead_letter_queue
                   (task_id, type, project, payload, required_capabilities,
                    final_error, retry_count, max_retries, failed_at, agent_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (task_id, task["type"], task["project"], task["payload"],
                 task["required_capabilities"], error, retries, max_retries,
                 now_iso(), agent_id or task.get("assigned_to")),
            )

        conn.execute("UPDATE agents SET current_task_id=NULL WHERE current_task_id=?", (task_id,))
        conn.commit()
    finally:
        conn.close()
    return get_task(task_id)


# ── Agent heartbeat ────────────────────────────────────────────────────

# ── Event log helpers ────────────────────────────────────────────────────

# emit() and event_log calls disabled due to SQLite connection leak.
# Use log_event() inside transactions instead.

def upsert_agent_heartbeat(agent_id: str, hostname: str = None,
                           tailscale_ip: str = None, capabilities: list = None,
                           current_task_id: str = None) -> bool:
    conn = get_conn()
    existing = conn.execute("SELECT id, online FROM agents WHERE id=?", (agent_id,)).fetchone()
    was_offline = not existing or not existing["online"]
    ts = now_iso()
    if existing:
        updates = {"last_heartbeat": ts, "online": 1}
        if hostname:
            updates["hostname"] = hostname
        if tailscale_ip:
            updates["tailscale_ip"] = tailscale_ip
        if capabilities is not None:
            updates["capabilities"] = json.dumps(capabilities)
        if current_task_id is not None:
            updates["current_task_id"] = current_task_id
        set_clause = ", ".join(f"{k}=?" for k in updates)
        conn.execute(
            f"UPDATE agents SET {set_clause} WHERE id=?",
            list(updates.values()) + [agent_id],
        )
    else:
        conn.execute(
            """INSERT INTO agents
               (id, hostname, tailscale_ip, capabilities, online,
                last_heartbeat, current_task_id, registered_at)
               VALUES (?,?,?,?,1,?,?,?)""",
            (agent_id, hostname, tailscale_ip,
             json.dumps(capabilities or []), ts, current_task_id, ts),
        )
    conn.commit()
    conn.close()
    if was_offline:
        emit("agent_online", source=agent_id,
             details={"hostname": hostname, "capabilities": capabilities or []})
    return was_offline


def get_agents() -> list:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM agents ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_task_log(task_id: str) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM task_log WHERE task_id=? ORDER BY timestamp ASC",
        (task_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_stale_agents_offline(threshold_seconds: int = 90):
    conn = get_conn()
    stale = conn.execute(
        """SELECT id FROM agents WHERE online=1
           AND last_heartbeat < datetime('now', ?)""",
        (f"-{threshold_seconds} seconds",),
    ).fetchall()
    if stale:
        stale_ids = [r["id"] for r in stale]
        # Find any claimed/running tasks assigned to stale agents
        placeholders = ",".join(["?"] * len(stale_ids))
        orphaned = conn.execute(
            f"""SELECT id, retries FROM tasks
               WHERE status IN ('claimed','running')
                 AND assigned_to IN ({placeholders})""",
            stale_ids,
        ).fetchall()
        for t in orphaned:
            # Re-queue the task for retry
            conn.execute(
                """UPDATE tasks
                   SET status='pending',
                       assigned_to=NULL,
                       claimed_at=NULL,
                       retries=?
                   WHERE id=?""",
                (t["retries"], t["id"]),
            )
            log_event(conn, t["id"], "requeued", details="agent went stale")
        # Mark agents offline
        conn.execute(
            """UPDATE agents SET online=0, current_task_id=NULL
               WHERE online=1 AND last_heartbeat < datetime('now', ?)""",
            (f"-{threshold_seconds} seconds",),
        )
        conn.commit()
    conn.close()
    for row in stale:
        emit("agent_offline", source=row["id"])


# ── DLQ helpers ───────────────────────────────────────────────────────────

def get_dlq(project: str = None, limit: int = 100) -> list:
    conn = get_conn()
    where_clause = "WHERE project=?" if project else ""
    params = (project,) if project else ()
    rows = conn.execute(
        f"""SELECT * FROM dead_letter_queue
            {where_clause}
            ORDER BY failed_at DESC LIMIT ?""",
        params + (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def recover_dlq_entry(dlq_id: int) -> Optional[dict]:
    """Move a DLQ task back to pending."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM dead_letter_queue WHERE id=?", (dlq_id,),
    ).fetchone()
    if not row:
        conn.close()
        return None
    # Re-create task from DLQ
    conn.execute(
        """UPDATE tasks SET status='pending', retries=0, error=NULL,
           assigned_to=NULL, completed_at=NULL
           WHERE id=? AND status='failed'""",
        (row["task_id"],),
    )
    conn.execute(
        "UPDATE dead_letter_queue SET recovered_at=? WHERE id=?",
        (now_iso(), dlq_id),
    )
    conn.commit()
    conn.close()
    return get_task(row["task_id"])


# ── Attempt history helpers ───────────────────────────────────────────────

def get_task_attempts(task_id: str) -> list:
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM task_attempts
           WHERE task_id=? ORDER BY attempt_number ASC""",
        (task_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def log_attempt_completion(task_id: str, result_artifact: str = None) -> int:
    conn = get_conn()
    conn.execute(
        """UPDATE task_attempts SET status='completed', finished_at=?, result_artifact=?
           WHERE task_id=? AND status='started'
           ORDER BY attempt_number DESC LIMIT 1""",
        (now_iso(), result_artifact, task_id),
    )
    changed = conn.total_changes
    conn.commit()
    conn.close()
    return changed


# ── A2A messaging ──────────────────────────────────────────────────────

def send_a2a_message(from_agent: str, to_agent: Optional[str],
                     message_type: str, payload: dict,
                     task_id: str = None) -> str:
    msg_id = str(uuid.uuid4())
    conn = get_conn()
    conn.execute(
        """INSERT INTO a2a_messages
           (id, from_agent, to_agent, message_type, payload, status, created_at, task_id)
           VALUES (?,?,?,?,?,?,?,?)""",
        (msg_id, from_agent, to_agent, message_type,
         json.dumps(payload), "sent", now_iso(), task_id),
    )
    conn.commit()
    conn.close()
    emit("a2a_send", source=from_agent, target=to_agent, task_id=task_id,
         details={"message_id": msg_id, "message_type": message_type,
                  "payload_preview": str(payload)[:120]})
    return msg_id


def get_a2a_inbox(agent_id: str, unread_only: bool = True) -> list:
    conn = get_conn()
    clause = "WHERE (to_agent=? OR to_agent IS NULL) AND status!='read'" if unread_only else \
             "WHERE (to_agent=? OR to_agent IS NULL)"
    rows = conn.execute(
        f"SELECT * FROM a2a_messages {clause} ORDER BY created_at ASC",
        (agent_id,),
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["payload"] = json.loads(d["payload"] or "{}")
        except Exception:
            pass
        result.append(d)
    return result


def ack_a2a_message(msg_id: str) -> bool:
    conn = get_conn()
    conn.execute("UPDATE a2a_messages SET status='read' WHERE id=?", (msg_id,))
    changed = conn.total_changes > 0
    conn.commit()
    conn.close()
    return changed


def get_a2a_recent(limit: int = 50) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM a2a_messages ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["payload"] = json.loads(d["payload"] or "{}")
        except Exception:
            pass
        result.append(d)
    return result


# ── Tool call logging ──────────────────────────────────────────────────

def log_tool_call(agent_id: str, task_id: str, tool_name: str,
                  tool_input: dict) -> int:
    return emit("tool_call", source=agent_id, task_id=task_id,
                details={"tool": tool_name, "input": tool_input, "status": "started"})


def log_tool_result(agent_id: str, task_id: str, tool_name: str,
                    tool_output: dict, duration_ms: int, success: bool = True):
    emit("tool_result", source=agent_id, task_id=task_id,
         details={"tool": tool_name, "output": tool_output,
                  "duration_ms": duration_ms, "success": success})
