import json
import logging
import os
import signal
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)


@dataclass
class Task:
    id: str
    type: str
    payload: dict
    priority: int = 1
    project: str = "LISA_FTM"
    target_machine: Optional[str] = None
    retries: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        payload = d.get("payload", {})
        if isinstance(payload, str):
            payload = json.loads(payload)
        return cls(
            id=d["id"], type=d["type"], payload=payload,
            priority=d.get("priority", 1), project=d.get("project", "LISA_FTM"),
            target_machine=d.get("target_machine"), retries=d.get("retries", 0),
        )


@dataclass
class Message:
    id: str
    from_agent: str
    to_agent: Optional[str]
    message_type: str
    payload: dict
    task_id: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "Message":
        payload = d.get("payload", {})
        if isinstance(payload, str):
            payload = json.loads(payload)
        return cls(
            id=d["id"], from_agent=d["from_agent"], to_agent=d.get("to_agent"),
            message_type=d.get("message_type", "message"), payload=payload,
            task_id=d.get("task_id"),
        )


class Agent:
    def __init__(self, agent_id: str, coordinator_url: str, capabilities: list[str],
                 token: Optional[str] = None):
        self.agent_id = agent_id
        self.url = coordinator_url.rstrip("/")
        self.capabilities = capabilities
        self.running = False
        self._task_handlers: dict[str, Callable] = {}
        self._message_handlers: dict[str, Callable] = {}
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._current_task_id: Optional[str] = None
        # Shared-secret auth; coordinator enforces it when DIGI_OFFICE_TOKEN
        # is set on its side. requests.Session applies it to every call.
        self.token = token or os.environ.get("DIGI_OFFICE_TOKEN")
        self._http = requests.Session()
        if self.token:
            self._http.headers["Authorization"] = f"Bearer {self.token}"

    # ── Decorators ────────────────────────────────────────────────────

    def task_handler(self, task_type: str):
        def decorator(fn: Callable):
            self._task_handlers[task_type] = fn
            return fn
        return decorator

    def message_handler(self, message_type: str):
        """Handle incoming A2A messages: @agent.message_handler("status_query")"""
        def decorator(fn: Callable):
            self._message_handlers[message_type] = fn
            return fn
        return decorator

    # ── Lifecycle ─────────────────────────────────────────────────────

    def register(self) -> bool:
        return self._send_heartbeat()

    def run(self, poll_interval: int = 5, register_retries: int = 0):
        """
        Main loop. register_retries=0 retries forever with capped backoff —
        an agent must survive the coordinator restarting or coming up later,
        not exit and leave its host out of the fleet until someone notices.
        """
        self.running = True
        signal.signal(signal.SIGTERM, self._handle_sigterm)
        signal.signal(signal.SIGINT, self._handle_sigterm)

        attempt = 0
        backoff = 2
        while not self.register():
            attempt += 1
            if register_retries and attempt >= register_retries:
                logger.error("Registration failed after %d attempts — giving up (%s)",
                             attempt, self.url)
                return
            if not self.running:
                return
            logger.warning("Coordinator unreachable at %s — retrying in %ds", self.url, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

        logger.info("Agent %s registered. Polling every %ds", self.agent_id, poll_interval)
        self._start_heartbeat_thread()

        while self.running:
            task = self.claim_task()
            if task:
                self._execute(task)
            else:
                time.sleep(poll_interval)

        logger.info("Agent %s shutting down", self.agent_id)

    # ── Task operations ────────────────────────────────────────────────

    def claim_task(self) -> Optional[Task]:
        try:
            resp = self._http.get(
                f"{self.url}/tasks/claim",
                params={"agent_id": self.agent_id,
                        "capabilities": json.dumps(self.capabilities)},
                timeout=10,
            )
            if resp.status_code == 200:
                return Task.from_dict(resp.json())
            return None
        except Exception as e:
            logger.warning("Claim failed: %s", e)
            return None

    def complete_task(self, task_id: str, result: dict):
        try:
            resp = self._http.post(f"{self.url}/tasks/{task_id}/complete",
                                 json={"result_payload": result,
                                       "agent_id": self.agent_id}, timeout=10)
            if resp.status_code == 409:
                logger.error("Task %s was reassigned while we ran it — result discarded "
                             "by coordinator (fencing). %s", task_id[:8], resp.text)
        except Exception as e:
            logger.warning("complete_task failed: %s", e)

    def fail_task(self, task_id: str, error: str):
        try:
            resp = self._http.post(f"{self.url}/tasks/{task_id}/fail",
                                 json={"error": error,
                                       "agent_id": self.agent_id}, timeout=10)
            if resp.status_code == 409:
                logger.error("Task %s was reassigned while we ran it — failure report "
                             "discarded by coordinator (fencing).", task_id[:8])
        except Exception as e:
            logger.warning("fail_task failed: %s", e)

    def task_progress(self, task_id: str, progress: str, log_entry: str = None):
        """Emit a progress update visible in the dashboard feed."""
        try:
            self._http.post(f"{self.url}/tasks/{task_id}/heartbeat",
                          json={"agent_id": self.agent_id,
                                "progress": progress,
                                "log_entry": log_entry or progress},
                          timeout=5)
        except Exception:
            pass

    # ── Tool call context manager ──────────────────────────────────────

    @contextmanager
    def tool_call(self, task_id: str, tool_name: str, tool_input: dict = None):
        """
        Context manager that logs tool start/end to the coordinator feed.
        Usage:
            with agent.tool_call(task.id, "embed_texts", {"n": 500}) as tc:
                result = do_embedding(...)
                tc["output"] = result
        """
        input_data = tool_input or {}
        ctx = {"output": {}, "success": True}
        t0 = time.monotonic()
        try:
            self._http.post(f"{self.url}/tasks/{task_id}/tool_call",
                          json={"agent_id": self.agent_id,
                                "tool_name": tool_name,
                                "tool_input": input_data},
                          timeout=5)
        except Exception:
            pass
        try:
            yield ctx
        except Exception as exc:
            ctx["success"] = False
            ctx["output"] = {"error": str(exc)}
            raise
        finally:
            duration_ms = int((time.monotonic() - t0) * 1000)
            try:
                self._http.post(f"{self.url}/tasks/{task_id}/tool_result",
                              json={"agent_id": self.agent_id,
                                    "tool_name": tool_name,
                                    "tool_output": ctx.get("output", {}),
                                    "duration_ms": duration_ms,
                                    "success": ctx.get("success", True)},
                              timeout=5)
            except Exception:
                pass

    # ── A2A messaging ─────────────────────────────────────────────────

    def send_message(self, to_agent: Optional[str], message_type: str,
                     payload: dict, task_id: str = None):
        """Send an A2A message. to_agent=None broadcasts to all."""
        try:
            self._http.post(f"{self.url}/a2a/messages",
                          json={"from_agent": self.agent_id, "to_agent": to_agent,
                                "message_type": message_type, "payload": payload,
                                "task_id": task_id},
                          timeout=8)
        except Exception as e:
            logger.warning("send_message failed: %s", e)

    def check_inbox(self) -> list[Message]:
        """Poll inbox and dispatch to registered message handlers."""
        try:
            resp = self._http.get(f"{self.url}/a2a/inbox/{self.agent_id}", timeout=5)
            if resp.status_code != 200:
                return []
            msgs = [Message.from_dict(m) for m in resp.json()]
            for msg in msgs:
                handler = self._message_handlers.get(msg.message_type) or \
                          self._message_handlers.get("*")
                if handler:
                    try:
                        handler(msg)
                    except Exception:
                        logger.exception("Message handler error for %s", msg.message_type)
                self._http.post(f"{self.url}/a2a/messages/{msg.id}/ack",
                              json={"agent_id": self.agent_id}, timeout=5)
            return msgs
        except Exception as e:
            logger.debug("check_inbox failed: %s", e)
            return []

    # ── Internal ──────────────────────────────────────────────────────

    def _execute(self, task: Task):
        handler = self._task_handlers.get(task.type)
        if not handler:
            self.fail_task(task.id, f"No handler for task type '{task.type}'")
            return

        self._current_task_id = task.id
        logger.info("Executing task %s (%s)", task.id[:8], task.type)

        # ── Memory: recall before execution (optional, no hard dependency) ──
        if hasattr(self, '_memory_con'):
            try:
                import agent_memory as am
                mems = am.recall(self._memory_con, task.type, k=3)
                if mems:
                    mem_str = " | ".join(f"[{m['entity']}] {m['summary'][:120]}" for m in mems)
                    logger.info("memory recall → %s", mem_str)
            except ImportError:
                pass

        try:
            result = handler(task)
            # ── Memory: capture result after success ──────────────────────
            if hasattr(self, '_memory_con'):
                try:
                    import agent_memory as am
                    am.capture(
                        self._memory_con,
                        content=f"task_id={task.id} type={task.type} "
                                f"result_keys={list((result or {}).keys()) if result else []}",
                        type="task_result",
                        entity="revalomon_tasks",
                        importance=0.6,
                    )
                except ImportError:
                    pass
            self.complete_task(task.id, result or {})
            logger.info("Task %s done", task.id[:8])
        except Exception as e:
            # ── Memory: capture error ─────────────────────────────────────
            if hasattr(self, '_memory_con'):
                try:
                    import agent_memory as am
                    am.capture(
                        self._memory_con,
                        content=f"task_id={task.id} type={task.type} ERROR: {e}",
                        type="task_error",
                        entity="revalomon_tasks",
                        importance=0.85,
                    )
                except ImportError:
                    pass
            logger.exception("Task %s failed", task.id[:8])
            self.fail_task(task.id, str(e))
        finally:
            self._current_task_id = None

    def _send_heartbeat(self) -> bool:
        try:
            self._http.post(
                f"{self.url}/agents/{self.agent_id}/heartbeat",
                json={"agent_id": self.agent_id,
                      "hostname": socket.gethostname(),
                      "capabilities": self.capabilities,
                      "current_task_id": self._current_task_id},
                timeout=10,
            )
            return True
        except Exception as e:
            logger.warning("Heartbeat failed: %s", e)
            return False

    def _start_heartbeat_thread(self):
        def loop():
            while self.running:
                time.sleep(25)
                if self.running:
                    self._send_heartbeat()
                    self.check_inbox()
        self._heartbeat_thread = threading.Thread(target=loop, daemon=True)
        self._heartbeat_thread.start()

    def _start_inbox_thread(self, poll_interval: int = 10):
        """Dedicated inbox polling — faster than 25s heartbeat."""
        def loop():
            while self.running:
                time.sleep(poll_interval)
                if self.running:
                    self.check_inbox()
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        return t

    def _handle_sigterm(self, signum, frame):
        logger.info("Signal %s received — stopping", signum)
        self.running = False
