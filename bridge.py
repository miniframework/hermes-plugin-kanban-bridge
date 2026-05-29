"""Bridge — hub-mode task polling and result forwarding."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)


class HubBridge:
    """Background thread: polls Hub for pending tasks, accepts them,
    monitors local execution, and sends results back."""

    _active_instance: Optional["HubBridge"] = None

    def __init__(self, hub_client, poll_interval: int = 5, self_name: str = "",
                 approval_required: bool = False, approval_channel: str = ""):
        if HubBridge._active_instance is not None:
            HubBridge._active_instance.stop()
            HubBridge._active_instance = None
        self._hub = hub_client
        self._interval = poll_interval
        self._self_name = self_name
        self._approval_required = approval_required
        self._approval_channel = approval_channel
        self._pending_approvals: Dict[str, str] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._hub_to_local: Dict[str, str] = {}
        self._local_to_hub: Dict[str, str] = {}
        self._accepted_on_hub: set = set()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._rebuild_mappings()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="kanban-hub-bridge")
        self._thread.start()
        HubBridge._active_instance = self
        log.info("hub-bridge started (interval=%ds, recovered=%d mappings, approval=%s, channel=%s)",
                 self._interval, len(self._hub_to_local), self._approval_required, self._approval_channel)

    def _rebuild_mappings(self):
        """Recover hub↔local mappings from DB after restart."""
        try:
            from hermes_cli import kanban_db
            with kanban_db.connect() as conn:
                rows = conn.execute(
                    "SELECT id, idempotency_key, status FROM tasks "
                    "WHERE idempotency_key LIKE 'hub:%' "
                    "AND status NOT IN ('gave_up', 'archived')"
                ).fetchall()
                for row in rows:
                    local_id = row["id"] if isinstance(row, dict) else row[0]
                    ikey = row["idempotency_key"] if isinstance(row, dict) else row[1]
                    status = row["status"] if isinstance(row, dict) else row[2]
                    hub_id = ikey[4:]

                    if status == "done":
                        if not self._approval_required:
                            continue
                        hub_task = self._hub.get_task(hub_id)
                        hub_status = hub_task.get("status", "") if hub_task else ""
                        if hub_status in ("completed", "acked"):
                            continue

                    self._hub_to_local[hub_id] = local_id
                    self._local_to_hub[local_id] = hub_id
            if self._hub_to_local:
                log.info("hub-bridge: rebuilt %d mappings from DB", len(self._hub_to_local))
        except Exception:
            log.exception("hub-bridge: failed to rebuild mappings")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _run(self):
        while not self._stop.is_set():
            try:
                self._poll_and_accept()
                self._check_local_completions()
                self._poll_discord_approvals()
            except Exception:
                log.exception("hub-bridge tick failed")
            self._stop.wait(self._interval)

    def _poll_and_accept(self):
        """Pull pending tasks from Hub, create local kanban tasks, accept on Hub."""
        pending = self._hub.poll_pending()
        if not pending:
            return

        from hermes_cli import kanban_db

        for task in pending:
            hub_id = task.get("id", "")
            if hub_id in self._hub_to_local:
                continue

            result = self._hub.accept_task(hub_id)
            if not result:
                continue
            is_blocked = result.get("status") == "blocked"
            if not is_blocked:
                self._accepted_on_hub.add(hub_id)

            origin_task_id = task.get("origin_task_id", "")
            if task.get("origin_node") == self._self_name and origin_task_id:
                self._hub_to_local[hub_id] = origin_task_id
                self._local_to_hub[origin_task_id] = hub_id
                new_status = "blocked" if is_blocked else "ready"
                with kanban_db.connect() as conn:
                    conn.execute(
                        "UPDATE tasks SET assignee = ?, status = ? WHERE id = ?",
                        (task.get("target_worker", "default"), new_status, origin_task_id),
                    )
                    conn.commit()
                log.info("hub-bridge: %s %s → existing local %s",
                         "blocked" if is_blocked else "accepted", hub_id, origin_task_id)
                continue

            local_status = "blocked" if is_blocked else None
            with kanban_db.connect() as conn:
                local_id = kanban_db.create_task(
                    conn,
                    title=f"[hub:{task.get('origin_node', '?')}] {task.get('title', '')}",
                    body=task.get("body"),
                    assignee=task.get("target_worker", "default"),
                    priority=task.get("priority", 0),
                    created_by=f"hub:{task.get('origin_node', '?')}",
                    idempotency_key=f"hub:{hub_id}",
                )
                if is_blocked:
                    conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (local_id,))
                    conn.commit()

            self._hub_to_local[hub_id] = local_id
            self._local_to_hub[local_id] = hub_id
            log.info("hub-bridge: %s %s → local %s",
                     "blocked" if is_blocked else "accepted", hub_id, local_id)

    def _check_local_completions(self):
        """Check if any local tasks mapped to Hub tasks have finished."""
        if not self._local_to_hub:
            return

        from hermes_cli import kanban_db

        for local_id, hub_id in list(self._local_to_hub.items()):
            with kanban_db.connect() as conn:
                task = kanban_db.get_task(conn, local_id)
                if not task:
                    continue

                if task.status == "done":
                    log.info("hub-bridge: local %s is done, approval_required=%s, channel=%s",
                             local_id, self._approval_required, self._approval_channel)
                    runs = kanban_db.list_runs(conn, local_id) if hasattr(kanban_db, "list_runs") else []
                    summary = ""
                    metadata = {}
                    if runs:
                        last = runs[-1]
                        summary = getattr(last, "summary", "") or ""
                        started = getattr(last, "started_at", None)
                        finished = getattr(last, "finished_at", None)
                        if started and finished:
                            metadata["duration_s"] = round(finished - started, 2)
                        metadata["total_runs"] = len(runs)
                        metadata["run_id"] = getattr(last, "id", None)
                        tokens_in = getattr(last, "tokens_in", None)
                        tokens_out = getattr(last, "tokens_out", None)
                        if tokens_in is not None:
                            metadata["tokens_in"] = tokens_in
                        if tokens_out is not None:
                            metadata["tokens_out"] = tokens_out
                    metadata["local_task_id"] = local_id
                    metadata["worker"] = task.assignee or ""

                    if self._approval_required and self._approval_channel:
                        approval = self._pending_approvals.get(hub_id)
                        if approval is None:
                            if self._notify_approval(hub_id, task.title, summary):
                                self._pending_approvals[hub_id] = "pending"
                            continue
                        if approval == "pending":
                            continue
                        if approval == "rejected":
                            self._hub.fail_task(hub_id, reason="rejected by user")
                            del self._local_to_hub[local_id]
                            del self._hub_to_local[hub_id]
                            del self._pending_approvals[hub_id]
                            self._accepted_on_hub.discard(hub_id)
                            log.info("hub-bridge: rejected %s (hub %s)", local_id, hub_id)
                            continue

                        del self._pending_approvals[hub_id]

                    ok = self._hub.submit_result(hub_id, summary=summary, metadata=json.dumps(metadata))
                    if ok:
                        del self._local_to_hub[local_id]
                        del self._hub_to_local[hub_id]
                        self._accepted_on_hub.discard(hub_id)
                        log.info("hub-bridge: sent result for %s (hub %s)", local_id, hub_id)

                elif task.status == "ready" and hub_id not in self._accepted_on_hub:
                    result = self._hub.accept_task(hub_id)
                    if result and result.get("status") == "accepted":
                        self._accepted_on_hub.add(hub_id)
                        log.info("hub-bridge: unblocked %s (hub %s), now running", local_id, hub_id)
                    elif result:
                        log.debug("hub-bridge: retry accept %s (hub %s) still blocked", local_id, hub_id)

                elif task.status == "blocked":
                    reason = getattr(task, "last_failure_error", "") or "blocked"
                    self._hub.fail_task(hub_id, reason=reason)
                    log.info("hub-bridge: blocked %s (hub %s), keeping mapping", local_id, hub_id)

                elif task.status == "gave_up":
                    reason = getattr(task, "last_failure_error", "") or "gave_up"
                    self._hub.fail_task(hub_id, reason=reason)
                    log.info("hub-bridge: gave_up %s (hub %s), keeping mapping", local_id, hub_id)

    def _notify_approval(self, hub_id: str, title: str, summary: str) -> bool:
        """Send approval notification to Discord channel via gateway adapter."""
        msg = (
            f"📋 **Task completed — approval required**\n"
            f"**{title}**\n"
            f"Result: {summary[:500]}\n"
            f"ID: `{hub_id}`\n\n"
            f"Reply `approve {hub_id}` or `reject {hub_id}`"
        )
        try:
            import asyncio
            from gateway.run import _gateway_runner_ref
            runner = _gateway_runner_ref()
            if runner is None:
                log.warning("hub-bridge: gateway runner not available, cannot send approval notification")
                return False
            from gateway.config import Platform
            adapter = runner.adapters.get(Platform.DISCORD)
            if adapter is None:
                log.warning("hub-bridge: discord adapter not available")
                return False
            loop = adapter._client.loop if hasattr(adapter, "_client") and adapter._client else asyncio.get_event_loop()
            fut = asyncio.run_coroutine_threadsafe(
                adapter.send(chat_id=self._approval_channel, content=msg),
                loop,
            )
            fut.result(timeout=10)
            log.info("hub-bridge: sent approval notification for %s to channel %s", hub_id, self._approval_channel)
            return True
        except Exception:
            log.exception("hub-bridge: failed to send approval notification for %s", hub_id)
            return False

    def _get_discord_token(self) -> str:
        import os
        return os.environ.get("DISCORD_BOT_TOKEN", "").strip()

    def _discord_send(self, content: str) -> bool:
        import requests
        token = self._get_discord_token()
        if not token:
            log.warning("hub-bridge: DISCORD_BOT_TOKEN not set")
            return False
        r = requests.post(
            f"https://discord.com/api/v10/channels/{self._approval_channel}/messages",
            headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
            json={"content": content},
            timeout=10,
        )
        if r.ok:
            log.info("hub-bridge: sent approval notification to channel %s", self._approval_channel)
            return True
        log.warning("hub-bridge: discord send failed %d: %s", r.status_code, r.text[:200])
        return False

    def _poll_discord_approvals(self):
        """Poll Discord channel for approve/reject messages."""
        if not self._pending_approvals or not self._approval_channel:
            return
        import re
        import requests
        token = self._get_discord_token()
        if not token:
            return
        try:
            params = {"limit": 20}
            if hasattr(self, "_last_discord_msg_id") and self._last_discord_msg_id:
                params["after"] = self._last_discord_msg_id
            r = requests.get(
                f"https://discord.com/api/v10/channels/{self._approval_channel}/messages",
                headers={"Authorization": f"Bot {token}"},
                params=params,
                timeout=10,
            )
            if not r.ok:
                return
            messages = r.json()
            if not messages:
                return
            for msg in sorted(messages, key=lambda m: m["id"]):
                self._last_discord_msg_id = msg["id"]
                if msg.get("author", {}).get("bot"):
                    continue
                m = re.match(r"^(approve|reject)\s+(hub_\w+)$", msg.get("content", "").strip(), re.IGNORECASE)
                if not m:
                    continue
                decision = "approved" if m.group(1).lower() == "approve" else "rejected"
                hub_id = m.group(2)
                if self.resolve_approval(hub_id, decision):
                    emoji = "✅" if decision == "approved" else "❌"
                    self._discord_send(f"{emoji} {decision.title()} `{hub_id}`")
        except Exception:
            log.exception("hub-bridge: discord poll failed")

    def resolve_approval(self, hub_id: str, decision: str) -> bool:
        """Set approval decision for a hub task. Called by discord.py or slash commands."""
        if hub_id not in self._pending_approvals:
            return False
        self._pending_approvals[hub_id] = decision
        log.info("hub-bridge: approval for %s set to %s", hub_id, decision)
        return True

    @property
    def tracked_count(self) -> int:
        return len(self._hub_to_local)


class HubSubmitter:
    """Outbound: watches local tasks with hub: assignee prefix and submits to Hub."""

    _active_instance: Optional["HubSubmitter"] = None

    def __init__(self, hub_client, poll_interval: int = 5, plugin_ctx=None):
        if HubSubmitter._active_instance is not None:
            HubSubmitter._active_instance.stop()
            HubSubmitter._active_instance = None
        self._hub = hub_client
        self._interval = poll_interval
        self._ctx = plugin_ctx
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._submitted: Dict[str, str] = {}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="kanban-hub-submitter")
        self._thread.start()
        HubSubmitter._active_instance = self
        log.info("hub-submitter started")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _run(self):
        while not self._stop.is_set():
            try:
                self._scan_and_submit()
                self._poll_results()
            except Exception:
                log.exception("hub-submitter tick failed")
            self._stop.wait(self._interval)

    def _scan_and_submit(self):
        """Find local tasks with hub:node:worker assignee and submit to Hub."""
        from hermes_cli import kanban_db

        with kanban_db.connect() as conn:
            tasks = kanban_db.list_tasks(conn, status="ready")
            for task in tasks:
                assignee = task.assignee or ""
                if not assignee.startswith("hub:"):
                    continue
                if task.id in self._submitted:
                    continue

                parts = assignee.split(":", 2)
                if len(parts) < 3:
                    continue
                _, target_node, target_worker = parts

                r = self._hub.create_task(
                    target_node=target_node,
                    target_worker=target_worker,
                    title=task.title,
                    body=task.body,
                    priority=task.priority,
                    origin_task_id=task.id,
                )
                if r:
                    hub_status = r.get("status", "")
                    if hub_status in ("blocked", "failed"):
                        reason = r.get("reason", "worker at capacity" if hub_status == "blocked" else "failed on hub")
                        conn.execute(
                            "UPDATE tasks SET status = 'blocked' WHERE id = ?",
                            (task.id,),
                        )
                        conn.commit()
                        log.info("hub-submitter: %s %s on hub: %s", task.id, hub_status, reason)
                        if self._ctx:
                            self._ctx.inject_message(
                                f"[kanban-bridge] Task {task.id} blocked: {reason}",
                                role="system",
                            )
                        continue
                    self._submitted[task.id] = r.get("id", "")
                    conn.execute(
                        "UPDATE tasks SET status = 'running' WHERE id = ?",
                        (task.id,),
                    )
                    conn.commit()
                    log.info("hub-submitter: submitted %s → hub %s", task.id, r.get("id"))

    def _poll_results(self):
        """Check Hub for completed results and update local tasks."""
        from hermes_cli import kanban_db

        results = self._hub.get_results()
        if not results:
            return

        for r in results:
            origin_task_id = r.get("origin_task_id", "")
            if not origin_task_id:
                continue

            hub_id = r.get("id", "")
            status = r.get("status", "")

            log.debug("hub-submitter: poll got hub_id=%s origin=%s status=%s", hub_id, origin_task_id, status)

            with kanban_db.connect() as conn:
                local_task = kanban_db.get_task(conn, origin_task_id)
                if not local_task:
                    log.debug("hub-submitter: no local task for %s, skipping", origin_task_id)
                    continue

                log.debug("hub-submitter: local task %s status=%s", origin_task_id, local_task.status)

                if local_task.status in ("done", "gave_up", "blocked", "archived"):
                    if local_task.status == "blocked":
                        log.debug("hub-submitter: NOT acking hub %s (local is blocked), skipping", hub_id)
                    else:
                        if local_task.status == "done" and status == "failed":
                            summary = getattr(local_task, "result", "") or ""
                            log.info("hub-submitter: local %s done but hub %s failed, syncing completion back", origin_task_id, hub_id)
                            self._hub.submit_result(hub_id, summary=summary)
                        log.info("hub-submitter: acking hub %s (local %s already terminal)", hub_id, local_task.status)
                        self._hub.ack_task(hub_id)
                    self._submitted.pop(origin_task_id, None)
                    continue

                if status == "completed":
                    kanban_db.complete_task(
                        conn, origin_task_id,
                        summary=r.get("result_summary", ""),
                    )
                elif status == "failed":
                    kanban_db.block_task(
                        conn, origin_task_id,
                        reason=r.get("result_summary", "failed on remote"),
                    )
                    self._submitted.pop(origin_task_id, None)
                    log.info("hub-submitter: got result for %s (failed, not acking)", origin_task_id)
                    if self._ctx:
                        summary_text = r.get("result_summary", "") or ""
                        self._ctx.inject_message(
                            f"[kanban-bridge] Task {origin_task_id} failed.{(' Summary: ' + summary_text) if summary_text else ''}",
                            role="system",
                        )
                    continue

            log.info("hub-submitter: ACKING hub %s for origin %s (completed path)", hub_id, origin_task_id)
            self._hub.ack_task(hub_id)
            self._submitted.pop(origin_task_id, None)
            log.info("hub-submitter: got result for %s (%s)", origin_task_id, status)

            if self._ctx:
                summary_text = r.get("result_summary", "") or ""
                notify = (
                    f"[kanban-bridge] Task {origin_task_id} {status}."
                    f"{(' Summary: ' + summary_text) if summary_text else ''}"
                )
                self._ctx.inject_message(notify, role="system")

    @property
    def tracked_count(self) -> int:
        return len(self._submitted)


_TERMINAL_KINDS = {"completed", "blocked", "gave_up", "crashed", "timed_out"}


class LocalTaskWatcher:
    """Polls local kanban task_events for terminal events and notifies the agent session.

    Covers local tasks (same node) that don't go through Hub.
    Hub tasks are already handled by HubSubmitter.inject_message.
    """

    _active_instance: Optional["LocalTaskWatcher"] = None

    def __init__(self, plugin_ctx, poll_interval: int = 5):
        if LocalTaskWatcher._active_instance is not None:
            LocalTaskWatcher._active_instance.stop()
            LocalTaskWatcher._active_instance = None
        self._ctx = plugin_ctx
        self._interval = poll_interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_event_id: int = 0
        self._hub_task_ids: set = set()

    def set_hub_task_ids(self, hub_local_to_hub: Dict[str, str], submitted: Dict[str, str]):
        """References to HubBridge/HubSubmitter mappings to skip Hub-managed tasks."""
        self._bridge_map = hub_local_to_hub
        self._submitter_map = submitted

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._init_cursor()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="kanban-local-watcher")
        self._thread.start()
        LocalTaskWatcher._active_instance = self
        log.info("local-task-watcher started (interval=%ds)", self._interval)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _init_cursor(self):
        try:
            from hermes_cli import kanban_db
            with kanban_db.connect() as conn:
                row = conn.execute("SELECT MAX(id) FROM task_events").fetchone()
                self._last_event_id = (row[0] or 0) if row else 0
        except Exception:
            log.debug("local-task-watcher: could not init cursor")

    def _run(self):
        while not self._stop.is_set():
            try:
                self._poll_events()
            except Exception:
                log.exception("local-task-watcher tick failed")
            self._stop.wait(self._interval)

    def _is_hub_managed(self, task_id: str) -> bool:
        if hasattr(self, "_bridge_map") and task_id in self._bridge_map:
            return True
        if hasattr(self, "_submitter_map") and task_id in self._submitter_map:
            return True
        return False

    def _poll_events(self):
        from hermes_cli import kanban_db

        with kanban_db.connect() as conn:
            rows = conn.execute(
                "SELECT id, task_id, kind, payload FROM task_events "
                "WHERE id > ? ORDER BY id",
                (self._last_event_id,),
            ).fetchall()

        if not rows:
            return

        for row in rows:
            event_id = row[0] if not isinstance(row, dict) else row["id"]
            task_id = row[1] if not isinstance(row, dict) else row["task_id"]
            kind = row[2] if not isinstance(row, dict) else row["kind"]
            payload = row[3] if not isinstance(row, dict) else row["payload"]

            self._last_event_id = event_id

            if kind not in _TERMINAL_KINDS:
                continue
            if self._is_hub_managed(task_id):
                continue

            summary = ""
            if payload:
                try:
                    p = json.loads(payload)
                    summary = p.get("summary", "") or p.get("reason", "") or ""
                except (json.JSONDecodeError, TypeError):
                    pass

            notify = (
                f"[kanban] Task {task_id} {kind}."
                f"{(' ' + summary[:200]) if summary else ''}"
            )
            self._ctx.inject_message(notify, role="system")
