import os
import sqlite3
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path(os.environ.get(
    "DIGI_OFFICE_DB",
    str(Path(__file__).parent.parent / "data" / "digioffice.db"),
))


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

        -- Per-recipient read receipts. The single status column above cannot
        -- represent broadcast (to_agent IS NULL) reads: the first reader's ack
        -- would hide the message from every other agent.
        CREATE TABLE IF NOT EXISTS a2a_reads (
            msg_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            read_at TEXT,
            PRIMARY KEY (msg_id, agent_id)
        );

        -- Backwards-compatible task_log alias
        CREATE TABLE IF NOT EXISTS task_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            event TEXT,
            agent_id TEXT,
            timestamp TEXT,
            details TEXT
        );

        -- Task attempt history (per-try audit trail)
        CREATE TABLE IF NOT EXISTS task_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL,
            agent_id TEXT,
            started_at TEXT,
            ended_at TEXT,
            status TEXT CHECK(status IN ('started','completed','failed')) DEFAULT 'started',
            error TEXT,
            result_artifact TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_task_attempts_task ON task_attempts(task_id, attempt_number);

        -- Goals: units of intent the planner decomposes into task pipelines
        CREATE TABLE IF NOT EXISTS goals (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT,
            acceptance TEXT,
            status TEXT CHECK(status IN ('active','done','blocked','failed'))
                   DEFAULT 'active',
            created_by TEXT,
            notes TEXT DEFAULT '',
            created_at TEXT, updated_at TEXT, completed_at TEXT
        );

        -- Dead Letter Queue: tasks that exhausted all retries
        CREATE TABLE IF NOT EXISTS dead_letter (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_task_id TEXT UNIQUE,
            type TEXT,
            project TEXT,
            priority INTEGER,
            payload TEXT,
            required_capabilities TEXT,
            error TEXT,
            retries INTEGER,
            max_retries INTEGER,
            first_attempt TEXT,
            last_attempt TEXT,
            moved_at TEXT,
            assigned_to TEXT,
            assigned_at TEXT,
            target_machine TEXT
        );
    """)
    # Additive migration for live databases (CREATE TABLE IF NOT EXISTS won't
    # add columns to an existing tasks table).
    for ddl in ("ALTER TABLE tasks ADD COLUMN goal_id TEXT",
                "ALTER TABLE tasks ADD COLUMN depends_on TEXT DEFAULT '[]'"):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # column already exists
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
                project: str = "LISA_FTM", max_retries: Optional[int] = None,
                goal_id: Optional[str] = None,
                depends_on: Optional[list] = None) -> dict:
    task_id = str(uuid.uuid4())
    conn = get_conn()
    cols = ["id", "type", "project", "priority", "payload", "required_capabilities",
            "status", "created_at", "target_machine", "goal_id", "depends_on"]
    vals = [task_id, type_, project, priority,
            json.dumps(payload),
            json.dumps(required_capabilities or []),
            "pending", now_iso(), target_machine,
            goal_id, json.dumps(depends_on or [])]
    if max_retries is not None:
        cols.append("max_retries")
        vals.append(max_retries)
    conn.execute(
        f"""INSERT INTO tasks
           ({','.join(cols)})
           VALUES ({','.join('?' * len(cols))})""",
        vals,
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
    if not row:
        return None
    task = dict(row)
    # Workers submit result_payload but the column is result_artifact (a JSON
    # string); consumers reading task["result"] saw None even on completed
    # tasks (Sprint 5.5 "result=None" bug). Expose the parsed value.
    try:
        task["result"] = json.loads(task["result_artifact"]) if task["result_artifact"] else None
    except (TypeError, ValueError):
        task["result"] = task["result_artifact"]
    return task


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
            SELECT id, type, payload, required_capabilities, target_machine,
                   retries, depends_on
            FROM tasks
            WHERE status = 'pending'
              AND (scheduled_at IS NULL OR scheduled_at <= datetime('now'))
            ORDER BY priority DESC, created_at ASC
        """).fetchall()

        for row in rows:
            required = json.loads(row["required_capabilities"])
            if required and not all(cap in capabilities for cap in required):
                continue  # Skip — agent doesn't have the required capabilities

            # Dependency gating: claimable only when every dependency is done.
            # Unknown/missing dependency ids count as unmet (defensive).
            try:
                deps = json.loads(row["depends_on"] or "[]")
            except (TypeError, ValueError):
                deps = []
            if deps:
                ph = ",".join("?" * len(deps))
                done = conn.execute(
                    f"SELECT COUNT(*) AS n FROM tasks WHERE id IN ({ph}) AND status='done'",
                    deps,
                ).fetchone()["n"]
                if done < len(deps):
                    continue  # Skip — pipeline stage not reached yet

            conn.execute(
                """UPDATE tasks SET status='claimed', assigned_to=?, claimed_at=?
                   WHERE id=? AND status='pending'""",
                (agent_id, now_iso(), row["id"]),
            )
            if conn.total_changes == 0:
                continue  # Race — someone else claimed it

            log_event(conn, row["id"], "claimed", agent_id)
            # Log attempt start
            conn.execute(
                """INSERT INTO task_attempts (task_id, attempt_number, agent_id, started_at, status)
                   VALUES (?,?,?,?,?)""",
                (row["id"], row["retries"] + 1, agent_id, now_iso(), "started"),
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


class NotTaskOwner(Exception):
    """Raised when an agent reports on a task that has been reassigned."""


def complete_task(task_id: str, result_artifact: str = None,
                  result_payload: dict = None, agent_id: str = None) -> Optional[dict]:
    conn = get_conn()
    try:
        task_row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        task = dict(task_row) if task_row else None
        if not task:
            return None
        if task["status"] == "done":
            return get_task(task_id)  # idempotent re-complete
        # Fencing: a stale agent whose task was reclaimed and reassigned must
        # not overwrite the new owner's run. Only enforced when the caller
        # identifies itself (backward compatible with older workers).
        if agent_id and task["assigned_to"] and task["assigned_to"] != agent_id:
            raise NotTaskOwner(
                f"task {task_id} is assigned to {task['assigned_to']}, not {agent_id}")
        # `is not None`: an empty-dict result ({} is falsy) was silently
        # dropped, storing NULL — workers that legitimately had nothing to
        # report looked like the result-reporting bug.
        artifact = result_artifact if result_artifact is not None else (
            json.dumps(result_payload) if result_payload is not None else None)
        conn.execute(
            """UPDATE tasks SET status='done', completed_at=?, result_artifact=?
               WHERE id=?""",
            (now_iso(), artifact, task_id),
        )
        conn.execute("UPDATE agents SET current_task_id=NULL WHERE current_task_id=?", (task_id,))
        log_event(conn, task_id, "completed")
        # Mark active attempt as completed
        row = conn.execute(
            "SELECT id FROM task_attempts WHERE task_id=? AND status='started' ORDER BY attempt_number DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE task_attempts SET ended_at=?, status='completed', result_artifact=? WHERE id=?",
                (now_iso(), artifact, row["id"]),
            )
        conn.commit()
    finally:
        conn.close()

    emit("task_done", source=task["assigned_to"] if task else None, task_id=task_id,
         details={"type": task["type"] if task else None})
    return get_task(task_id)


def fail_task(task_id: str, error: str, agent_id: str = None) -> Optional[dict]:
    conn = get_conn()
    try:
        task_row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        task = dict(task_row) if task_row else None
        if not task:
            return None
        # Same fencing as complete_task: a reclaimed agent's failure report
        # must not burn a retry of the new owner's attempt.
        if agent_id and task["assigned_to"] and task["assigned_to"] != agent_id:
            raise NotTaskOwner(
                f"task {task_id} is assigned to {task['assigned_to']}, not {agent_id}")

        retries = task["retries"] + 1
        max_retries = task["max_retries"]

        if retries < max_retries:
            delay_minutes = 2 ** retries
            conn.execute(
                """UPDATE tasks SET status='pending', retries=?, error=?,
                   scheduled_at=datetime('now', ?), assigned_to=NULL
                   WHERE id=?""",
                (retries, error, f"+{delay_minutes} minutes", task_id),
            )
            log_event(conn, task_id, "retry", details=f"retry {retries}/{max_retries}: {error}")
        else:
            conn.execute(
                """UPDATE tasks SET status='failed', retries=?, error=?, completed_at=?
                   WHERE id=?""",
                (retries, error, now_iso(), task_id),
            )
            log_event(conn, task_id, "failed", details=error)
            # Move to DLQ
            conn.execute(
                """INSERT OR IGNORE INTO dead_letter
                   (original_task_id, type, project, priority, payload,
                    required_capabilities, error, retries, max_retries,
                    first_attempt, last_attempt, moved_at,
                    assigned_to, assigned_at, target_machine)
                   SELECT id, type, project, priority, payload,
                          required_capabilities, ?, ?, ?, created_at, ?, ?,
                          assigned_to, claimed_at, target_machine
                   FROM tasks WHERE id=?""",
                (error, retries, max_retries, now_iso(), now_iso(), task_id),
            )

        conn.execute("UPDATE agents SET current_task_id=NULL WHERE current_task_id=?", (task_id,))
        # Mark active attempt as failed
        row = conn.execute(
            "SELECT id FROM task_attempts WHERE task_id=? AND status='started' ORDER BY attempt_number DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if row:
            conn.execute(
                """UPDATE task_attempts
                   SET ended_at=?, status='failed', error=?,
                       agent_id=COALESCE(agent_id, ?)
                   WHERE id=?""",
                (now_iso(), error, agent_id, row["id"]),
            )
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
    # last_heartbeat is stored as ISO-8601 with 'T' ("...T12:00:00Z") while
    # datetime('now') yields "... 12:00:00". Comparing the raw strings is
    # lexicographic and 'T' > ' ', so same-day heartbeats ALWAYS compared as
    # newer and agents only ever went stale at UTC date rollover. Normalize
    # both sides through datetime() so the comparison is temporal.
    conn = get_conn()
    stale = conn.execute(
        """SELECT id FROM agents WHERE online=1
           AND datetime(last_heartbeat) < datetime('now', ?)""",
        (f"-{threshold_seconds} seconds",),
    ).fetchall()
    if stale:
        conn.execute(
            """UPDATE agents SET online=0
               WHERE online=1 AND datetime(last_heartbeat) < datetime('now', ?)""",
            (f"-{threshold_seconds} seconds",),
        )
        conn.commit()
    conn.close()
    for row in stale:
        emit("agent_offline", source=row["id"])


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
    """
    Unread state is tracked per recipient in a2a_reads. The old single
    status column meant the FIRST agent to ack a broadcast (to_agent IS NULL)
    hid it from every other agent — broadcasts reached exactly one reader.
    Broadcasts are also no longer delivered back to their sender.
    """
    conn = get_conn()
    base = "WHERE (to_agent=? OR (to_agent IS NULL AND from_agent != ?))"
    unread = """ AND NOT EXISTS (
                   SELECT 1 FROM a2a_reads r
                   WHERE r.msg_id = a2a_messages.id AND r.agent_id = ?)"""
    clause = base + (unread if unread_only else "")
    params = (agent_id, agent_id, agent_id) if unread_only else (agent_id, agent_id)
    rows = conn.execute(
        f"SELECT * FROM a2a_messages {clause} ORDER BY created_at ASC",
        params,
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


def ack_a2a_message(msg_id: str, agent_id: str = None) -> bool:
    conn = get_conn()
    if agent_id:
        conn.execute(
            "INSERT OR IGNORE INTO a2a_reads (msg_id, agent_id, read_at) VALUES (?,?,?)",
            (msg_id, agent_id, now_iso()),
        )
        # Keep the legacy status column meaningful for DIRECTED messages only;
        # flipping it on a broadcast would hide it from other recipients via
        # any legacy reader still filtering on status.
        conn.execute(
            "UPDATE a2a_messages SET status='read' WHERE id=? AND to_agent IS NOT NULL",
            (msg_id,),
        )
    else:
        # Legacy ack without agent identity: original behavior (directed only).
        conn.execute(
            "UPDATE a2a_messages SET status='read' WHERE id=? AND to_agent IS NOT NULL",
            (msg_id,),
        )
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


# ── Dead Letter Queue ──────────────────────────────────────────────────

def list_dlq(limit: int = 100) -> list:
    """Return dead letter entries ordered by when they were moved (newest first)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM dead_letter ORDER BY id DESC LIMIT ?", (limit,),
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        for field in ("payload", "required_capabilities"):
            try:
                d[field] = json.loads(d[field] or "[]")
            except Exception:
                pass
        result.append(d)
    return result


def get_dlq_entry(original_task_id: str) -> Optional[dict]:
    """Return a single DLQ entry by the original task ID."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM dead_letter WHERE original_task_id=?", (original_task_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    for field in ("payload", "required_capabilities"):
        try:
            d[field] = json.loads(d[field] or "[]")
        except Exception:
            pass
    return d


def requeue_from_dlq(original_task_id: str, max_retries: Optional[int] = None) -> dict:
    """
    Requeue a task from the DLQ. Creates a fresh pending task with a new UUID.
    Optionally overrides max_retries. Returns the new task dict.
    """
    entry = get_dlq_entry(original_task_id)
    if not entry:
        return {"ok": False, "error": "DLQ entry not found"}

    conn = get_conn()
    new_task_id = str(uuid.uuid4())
    override_retries = max_retries if max_retries is not None else (entry["max_retries"] or 3)
    conn.execute(
        """INSERT INTO tasks
           (id, type, project, priority, payload, required_capabilities,
            status, created_at, max_retries, target_machine)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (new_task_id, entry["type"], entry["project"], entry["priority"],
         entry["payload"], entry["required_capabilities"],
         "pending", now_iso(), override_retries, entry["target_machine"]),
    )
    conn.execute("DELETE FROM dead_letter WHERE original_task_id=?", (original_task_id,))
    conn.commit()
    conn.close()
    return {"ok": True, "new_task_id": new_task_id}


def release_task(task_id: str) -> Optional[dict]:
    """
    Release a task that an agent abandoned (went stale without completing).
    Resets it to pending so another agent can claim it.
    """
    conn = get_conn()
    try:
        task_row = conn.execute(
            "SELECT * FROM tasks WHERE id=? AND status IN ('claimed','running')", (task_id,),
        ).fetchone()
        if not task_row:
            return None
        task = dict(task_row)
        conn.execute(
            """UPDATE tasks SET status='pending', assigned_to=NULL,
               claimed_at=NULL, retries=retries
               WHERE id=?""",
            (task_id,),
        )
        log_event(conn, task_id, "released", details="agent went stale")
        conn.commit()
    finally:
        conn.close()
    return get_task(task_id)


def reclaim_stale_tasks(threshold_seconds: int = 90):
    """
    Find tasks stuck in claimed/running whose assigned agent has gone stale
    and route them through the normal failure path (retry with backoff, DLQ
    when exhausted). Runs inside the sweeper.

    Notes on the WHERE clause:
    - `a.id IS NULL` covers agents that claimed without ever heartbeating
      (unregistered): the LEFT JOIN yields NULL and `NULL OR NULL` filtered
      the row out, so their tasks rotted forever (coordinator audit 2026-06-06).
    - `datetime(...)` normalizes the ISO-'T' heartbeat format; raw string
      comparison never matched within the same UTC day.
    - Grace period on claimed_at so a task claimed a moment ago by an agent
      that has not heartbeated yet is not instantly reclaimed.
    """
    conn = get_conn()
    try:
        # 'proxy:%' owners are coordinator-internal threads, not heartbeating
        # agents — the sweeper must not reclaim them mid-run. Crashed proxy
        # runs are recovered at startup by reclaim_orphaned_proxy_tasks().
        stale_tasks = conn.execute(
            """SELECT t.id FROM tasks t
               LEFT JOIN agents a ON t.assigned_to = a.id
               WHERE t.status IN ('claimed', 'running')
                 AND t.assigned_to IS NOT NULL
                 AND t.assigned_to NOT LIKE 'proxy:%'
                 AND datetime(t.claimed_at) < datetime('now', ?)
                 AND (a.id IS NULL
                      OR a.online = 0
                      OR datetime(a.last_heartbeat) < datetime('now', ?))""",
            (f"-{threshold_seconds} seconds", f"-{threshold_seconds} seconds"),
        ).fetchall()
    finally:
        conn.close()

    # Route through fail_task so reclaims consume a retry, get exponential
    # backoff, close the open task_attempt, and dead-letter when exhausted —
    # a permanently crashing agent must not ping-pong a task forever.
    for row in stale_tasks:
        fail_task(row["id"], "reclaimed: assigned agent went stale")
    return len(stale_tasks)

def reassign_task(task_id: str, new_owner: str) -> None:
    """
    Hand a claimed task to a different owner. Used when the claim endpoint
    dispatches a proxy-routed task: the polling agent that happened to claim
    it never executes it, so leaving it as the owner corrupted ownership
    accounting (fencing, reclaim, dashboards).
    """
    conn = get_conn()
    conn.execute("UPDATE tasks SET assigned_to=? WHERE id=?", (new_owner, task_id))
    log_event(conn, task_id, "reassigned", details=f"owner={new_owner}")
    conn.commit()
    conn.close()


def reclaim_orphaned_proxy_tasks() -> int:
    """
    Proxy tasks run on daemon threads inside the coordinator process; if the
    coordinator dies mid-run those tasks are stuck in claimed/running with a
    'proxy:%' owner that no longer exists. Called once at startup — any such
    task is, by construction, dead. Routes through fail_task for retry/DLQ.
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT id FROM tasks
               WHERE status IN ('claimed', 'running')
                 AND assigned_to LIKE 'proxy:%'""",
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        fail_task(row["id"], "coordinator restarted during proxy execution")
    return len(rows)


def cancel_task(task_id: str, reason: str = "") -> Optional[dict]:
    """
    Terminal cancel for obsolete tasks (planner use: a pipeline was
    superseded). Unlike fail_task this never retries and never dead-letters.
    Only valid from pending/claimed/running; returns None otherwise.
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM tasks WHERE id=? AND status IN ('pending','claimed','running')",
            (task_id,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            """UPDATE tasks SET status='failed', error=?, completed_at=?,
               assigned_to=NULL WHERE id=?""",
            (f"cancelled: {reason}"[:500], now_iso(), task_id),
        )
        conn.execute("UPDATE agents SET current_task_id=NULL WHERE current_task_id=?", (task_id,))
        att = conn.execute(
            "SELECT id FROM task_attempts WHERE task_id=? AND status='started' ORDER BY attempt_number DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if att:
            conn.execute(
                "UPDATE task_attempts SET ended_at=?, status='failed', error=? WHERE id=?",
                (now_iso(), f"cancelled: {reason}"[:500], att["id"]),
            )
        log_event(conn, task_id, "cancelled", details=reason)
        conn.commit()
    finally:
        conn.close()
    emit("task_cancelled", task_id=task_id, details={"reason": reason})
    return get_task(task_id)


# ── Goals ───────────────────────────────────────────────────────────────

GOAL_STATUSES = ("active", "done", "blocked", "failed")


def create_goal(title: str, description: str = "", acceptance: str = "",
                created_by: str = "") -> dict:
    goal_id = str(uuid.uuid4())
    ts = now_iso()
    conn = get_conn()
    conn.execute(
        """INSERT INTO goals (id, title, description, acceptance, status,
           created_by, notes, created_at, updated_at)
           VALUES (?,?,?,?, 'active', ?, '', ?, ?)""",
        (goal_id, title, description, acceptance, created_by, ts, ts),
    )
    conn.commit()
    conn.close()
    emit("goal_created", source=created_by,
         details={"goal_id": goal_id, "title": title})
    return get_goal(goal_id)


def list_goals(status: Optional[str] = None) -> list:
    conn = get_conn()
    if status:
        rows = conn.execute(
            "SELECT * FROM goals WHERE status=? ORDER BY created_at DESC", (status,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM goals ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_goal(goal_id: str) -> Optional[dict]:
    """Goal + rollup: task counts by status, finished tasks' parsed results,
    and any DLQ entries belonging to this goal's tasks."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
    if not row:
        conn.close()
        return None
    goal = dict(row)
    tasks = conn.execute(
        "SELECT * FROM tasks WHERE goal_id=? ORDER BY created_at ASC", (goal_id,)
    ).fetchall()
    task_ids = [t["id"] for t in tasks]
    dlq = []
    if task_ids:
        ph = ",".join("?" * len(task_ids))
        dlq = [dict(d) for d in conn.execute(
            f"SELECT original_task_id, error, retries FROM dead_letter WHERE original_task_id IN ({ph})",
            task_ids,
        ).fetchall()]
    conn.close()

    counts = {}
    finished = []
    for t in tasks:
        t = dict(t)
        counts[t["status"]] = counts.get(t["status"], 0) + 1
        if t["status"] in ("done", "failed"):
            try:
                result = json.loads(t["result_artifact"]) if t["result_artifact"] else None
            except (TypeError, ValueError):
                result = t["result_artifact"]
            finished.append({"id": t["id"], "type": t["type"], "status": t["status"],
                             "error": t["error"], "result": result})
    goal["rollup"] = {"total": len(tasks), "by_status": counts,
                      "finished": finished, "dlq": dlq}
    return goal


def set_goal_status(goal_id: str, status: str, notes: str = "") -> Optional[dict]:
    """Update status and APPEND timestamped notes — the planner's decision log
    must be reconstructable, so notes are never overwritten."""
    if status not in GOAL_STATUSES:
        raise ValueError(f"invalid goal status {status!r}")
    conn = get_conn()
    row = conn.execute("SELECT status, notes FROM goals WHERE id=?", (goal_id,)).fetchone()
    if not row:
        conn.close()
        return None
    old = row["status"]
    ts = now_iso()
    new_notes = (row["notes"] or "")
    if notes:
        new_notes += f"\n[{ts}] ({old}→{status}) {notes}"
    completed = ts if status in ("done", "failed") else None
    conn.execute(
        """UPDATE goals SET status=?, notes=?, updated_at=?,
           completed_at=COALESCE(?, completed_at) WHERE id=?""",
        (status, new_notes, ts, completed, goal_id),
    )
    conn.commit()
    conn.close()
    emit("goal_status", details={"goal_id": goal_id, "from": old, "to": status,
                                 "notes": notes[:300]})
    return get_goal(goal_id)


# ── Task attempt history helpers ────────────────────────────────────────

def log_task_attempt(task_id: str, attempt_number: int, agent_id: str,
                       status: str = 'started', error: str = None,
                       result_artifact: str = None, ended_at: str = None):
    conn = get_conn()
    conn.execute(
        """INSERT INTO task_attempts
           (task_id, attempt_number, agent_id, started_at, ended_at, status, error, result_artifact)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT DO NOTHING""",
        (task_id, attempt_number, agent_id, now_iso(), ended_at, status, error, result_artifact),
    )
    conn.commit()
    conn.close()


def get_task_attempts(task_id: str) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM task_attempts WHERE task_id=? ORDER BY attempt_number ASC",
        (task_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_task_attempt_end(task_id: str, attempt_number: int,
                            status: str = 'completed', error: str = None,
                            result_artifact: str = None):
    conn = get_conn()
    conn.execute(
        """UPDATE task_attempts
           SET ended_at=?, status=?, error=?, result_artifact=?
           WHERE task_id=? AND attempt_number=?""",
        (now_iso(), status, error, result_artifact, task_id, attempt_number),
    )
    cn = conn.total_changes
    conn.commit()
    conn.close()
    return cn > 0
