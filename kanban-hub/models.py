"""Kanban Hub — SQLite data model and CRUD operations."""

from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional
import logging

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "hub.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    secret_hash TEXT NOT NULL,
    last_heartbeat INTEGER,
    status TEXT DEFAULT 'online',
    created_at INTEGER
);

CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES nodes(id),
    node_name TEXT NOT NULL,
    profile_name TEXT NOT NULL,
    description TEXT,
    max_concurrent INTEGER DEFAULT 5,
    active_tasks INTEGER DEFAULT 0,
    credits_per_task INTEGER DEFAULT 100,
    created_at INTEGER
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    token TEXT UNIQUE NOT NULL,
    credits INTEGER DEFAULT 10000,
    created_at INTEGER
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    origin_node TEXT NOT NULL,
    origin_task_id TEXT,
    target_node TEXT NOT NULL,
    target_worker TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT,
    priority INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    result_summary TEXT,
    result_metadata TEXT,
    created_at INTEGER,
    assigned_at INTEGER,
    completed_at INTEGER
);
"""


def _gen_id() -> str:
    return uuid.uuid4().hex[:12]


@contextmanager
def connect(db_path: Optional[Path] = None, readonly: bool = False):
    path = db_path or DB_PATH
    if readonly:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
    else:
        conn = sqlite3.connect(str(path), timeout=3)
        conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        if not readonly:
            conn.commit()
    except Exception:
        if not readonly:
            conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Optional[Path] = None):
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.close()


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def register_node(conn: sqlite3.Connection, *, name: str, secret_hash: str) -> dict:
    now = int(time.time())
    existing = conn.execute("SELECT id FROM nodes WHERE name = ?", (name,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE nodes SET secret_hash = ?, last_heartbeat = ?, status = 'online' WHERE name = ?",
            (secret_hash, now, name),
        )
        return {"id": existing["id"], "name": name, "action": "updated"}
    node_id = _gen_id()
    conn.execute(
        "INSERT INTO nodes (id, name, secret_hash, last_heartbeat, status, created_at) VALUES (?, ?, ?, ?, 'online', ?)",
        (node_id, name, secret_hash, now, now),
    )
    return {"id": node_id, "name": name, "action": "created"}


def heartbeat_node(conn: sqlite3.Connection, *, name: str) -> bool:
    now = int(time.time())
    cur = conn.execute("UPDATE nodes SET last_heartbeat = ?, status = 'online' WHERE name = ?", (now, name))
    return cur.rowcount > 0


def list_nodes(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM nodes ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def get_node_by_name(conn: sqlite3.Connection, name: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM nodes WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------

def share_worker(
    conn: sqlite3.Connection,
    *,
    node_name: str,
    profile_name: str,
    description: str = "",
    max_concurrent: int = 5,
    credits_per_task: int = 100,
) -> dict:
    node = get_node_by_name(conn, node_name)
    if not node:
        raise ValueError(f"node '{node_name}' not registered")

    existing = conn.execute(
        "SELECT id FROM workers WHERE node_name = ? AND profile_name = ?",
        (node_name, profile_name),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE workers SET description = ?, max_concurrent = ?, credits_per_task = ? WHERE id = ?",
            (description, max_concurrent, credits_per_task, existing["id"]),
        )
        return {"id": existing["id"], "action": "updated"}

    worker_id = _gen_id()
    now = int(time.time())
    conn.execute(
        "INSERT INTO workers (id, node_id, node_name, profile_name, description, max_concurrent, active_tasks, credits_per_task, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
        (worker_id, node["id"], node_name, profile_name, description, max_concurrent, credits_per_task, now),
    )
    return {"id": worker_id, "action": "created"}


def unshare_worker(conn: sqlite3.Connection, *, node_name: str, profile_name: str) -> bool:
    cur = conn.execute(
        "DELETE FROM workers WHERE node_name = ? AND profile_name = ?",
        (node_name, profile_name),
    )
    return cur.rowcount > 0


def list_workers(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM workers ORDER BY node_name, profile_name").fetchall()
    return [dict(r) for r in rows]


def get_worker(conn: sqlite3.Connection, node_name: str, profile_name: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM workers WHERE node_name = ? AND profile_name = ?",
        (node_name, profile_name),
    ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

def create_task(
    conn: sqlite3.Connection,
    *,
    origin_node: str,
    origin_task_id: Optional[str] = None,
    target_node: str,
    target_worker: str,
    title: str,
    body: Optional[str] = None,
    priority: int = 0,
) -> dict:
    if origin_task_id:
        existing = conn.execute(
            "SELECT id, status FROM tasks WHERE origin_node = ? AND origin_task_id = ? AND status NOT IN ('completed', 'failed')",
            (origin_node, origin_task_id),
        ).fetchone()
        if existing:
            return {"id": existing["id"], "status": existing["status"], "deduplicated": True}
    origin_user = conn.execute("SELECT id FROM users WHERE email = ?", (origin_node,)).fetchone()
    if not origin_user:
        task_id = f"hub_{_gen_id()}"
        now = int(time.time())
        conn.execute(
            "INSERT INTO tasks (id, origin_node, origin_task_id, target_node, target_worker, title, body, priority, status, result_summary, created_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'failed', ?, ?, ?)",
            (task_id, origin_node, origin_task_id, target_node, target_worker, title, body, priority,
             f"origin '{origin_node}' not registered", now, now),
        )
        return {"id": task_id, "status": "failed", "reason": f"origin '{origin_node}' not registered"}

    worker = conn.execute(
        "SELECT active_tasks, max_concurrent FROM workers WHERE node_name = ? AND profile_name = ?",
        (target_node, target_worker),
    ).fetchone()
    if not worker:
        task_id = f"hub_{_gen_id()}"
        now = int(time.time())
        conn.execute(
            "INSERT INTO tasks (id, origin_node, origin_task_id, target_node, target_worker, title, body, priority, status, result_summary, created_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'failed', ?, ?, ?)",
            (task_id, origin_node, origin_task_id, target_node, target_worker, title, body, priority,
             f"worker '{target_node}:{target_worker}' not found", now, now),
        )
        return {"id": task_id, "status": "failed", "reason": f"worker '{target_node}:{target_worker}' not found"}

    task_id = f"hub_{_gen_id()}"
    now = int(time.time())

    if worker["active_tasks"] >= worker["max_concurrent"]:
        conn.execute(
            "INSERT INTO tasks (id, origin_node, origin_task_id, target_node, target_worker, title, body, priority, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'blocked', ?)",
            (task_id, origin_node, origin_task_id, target_node, target_worker, title, body, priority, now),
        )
        return {"id": task_id, "status": "blocked", "reason": "worker at capacity"}

    conn.execute(
        "INSERT INTO tasks (id, origin_node, origin_task_id, target_node, target_worker, title, body, priority, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
        (task_id, origin_node, origin_task_id, target_node, target_worker, title, body, priority, now),
    )
    return {"id": task_id, "status": "pending"}


def list_pending_tasks(conn: sqlite3.Connection, *, node: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM tasks WHERE target_node = ? AND status = 'pending' ORDER BY priority DESC, created_at",
        (node,),
    ).fetchall()
    return [dict(r) for r in rows]


def accept_task(conn: sqlite3.Connection, task_id: str) -> bool:
    row = conn.execute("SELECT target_node, target_worker, status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row or row["status"] not in ("pending", "blocked"):
        return False

    worker = conn.execute(
        "SELECT active_tasks, max_concurrent FROM workers WHERE node_name = ? AND profile_name = ?",
        (row["target_node"], row["target_worker"]),
    ).fetchone()
    if worker and worker["active_tasks"] >= worker["max_concurrent"]:
        if row["status"] != "blocked":
            conn.execute(
                "UPDATE tasks SET status = 'blocked' WHERE id = ?",
                (task_id,),
            )
        return False

    now = int(time.time())
    conn.execute(
        "UPDATE tasks SET status = 'running', assigned_at = ? WHERE id = ? AND status IN ('pending', 'blocked')",
        (now, task_id),
    )
    if worker:
        conn.execute(
            "UPDATE workers SET active_tasks = active_tasks + 1 WHERE node_name = ? AND profile_name = ?",
            (row["target_node"], row["target_worker"]),
        )
    return True


def complete_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    summary: Optional[str] = None,
    metadata: Optional[str] = None,
) -> bool:
    now = int(time.time())
    cur = conn.execute(
        "UPDATE tasks SET status = 'completed', result_summary = ?, result_metadata = ?, completed_at = ? "
        "WHERE id = ? AND status IN ('running', 'failed', 'acked')",
        (summary, metadata, now, task_id),
    )
    if cur.rowcount > 0:
        row = conn.execute("SELECT origin_node, target_node, target_worker FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row:
            conn.execute(
                "UPDATE workers SET active_tasks = MAX(0, active_tasks - 1) WHERE node_name = ? AND profile_name = ?",
                (row["target_node"], row["target_worker"]),
            )
            worker = conn.execute(
                "SELECT credits_per_task FROM workers WHERE node_name = ? AND profile_name = ?",
                (row["target_node"], row["target_worker"]),
            ).fetchone()
            cost = worker["credits_per_task"] if worker else 100
            conn.execute("UPDATE users SET credits = credits - ? WHERE email = ?", (cost, row["origin_node"]))
            conn.execute("UPDATE users SET credits = credits + ? WHERE email = ?", (cost, row["target_node"]))
    return cur.rowcount > 0


def fail_task(conn: sqlite3.Connection, task_id: str, *, reason: str = "") -> bool:
    now = int(time.time())
    cur = conn.execute(
        "UPDATE tasks SET status = 'failed', result_summary = ?, completed_at = ? WHERE id = ? AND status IN ('pending', 'running')",
        (reason, now, task_id),
    )
    if cur.rowcount > 0:
        row = conn.execute("SELECT target_node, target_worker FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row:
            conn.execute(
                "UPDATE workers SET active_tasks = MAX(0, active_tasks - 1) WHERE node_name = ? AND profile_name = ?",
                (row["target_node"], row["target_worker"]),
            )
    return cur.rowcount > 0


def list_all_tasks(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def list_tasks_by_origin(conn: sqlite3.Connection, *, origin: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM tasks WHERE origin_node = ? ORDER BY created_at DESC",
        (origin,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_task(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def ack_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Mark a completed/failed task as acknowledged by origin node."""
    cur = conn.execute(
        "UPDATE tasks SET status = 'acked' WHERE id = ? AND status IN ('completed', 'failed')",
        (task_id,),
    )
    return cur.rowcount > 0


def get_results_for_origin(conn: sqlite3.Connection, *, origin: str) -> list[dict]:
    sql = "SELECT * FROM tasks WHERE origin_node = ? AND status IN ('completed', 'failed') ORDER BY completed_at"
    log.info("get_results_for_origin SQL: %s  params: [%s]", sql, origin)
    rows = conn.execute(sql, (origin,)).fetchall()
    results = [dict(r) for r in rows]
    for r in results:
        log.info("  -> id=%s status=%s result_summary=%s", r.get("id"), r.get("status"), r.get("result_summary"))
    return results


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def create_user(conn: sqlite3.Connection, *, email: str, password: str) -> dict:
    from werkzeug.security import generate_password_hash
    existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        raise ValueError("email already registered")
    user_id = _gen_id()
    token = uuid.uuid4().hex
    now = int(time.time())
    pw_hash = generate_password_hash(password)
    conn.execute(
        "INSERT INTO users (id, email, password_hash, token, credits, created_at) VALUES (?, ?, ?, ?, 10000, ?)",
        (user_id, email, pw_hash, token, now),
    )
    return {"id": user_id, "email": email, "token": token, "credits": 10000}


def authenticate_user(conn: sqlite3.Connection, *, email: str, password: str) -> Optional[dict]:
    from werkzeug.security import check_password_hash
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not row:
        return None
    if not check_password_hash(row["password_hash"], password):
        return None
    return dict(row)


def get_user_by_token(conn: sqlite3.Connection, token: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM users WHERE token = ?", (token,)).fetchone()
    return dict(row) if row else None


def get_user_by_email_and_token(conn: sqlite3.Connection, *, email: str, token: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM users WHERE email = ? AND token = ?", (email, token)).fetchone()
    return dict(row) if row else None


def add_credits(conn: sqlite3.Connection, user_id: str, amount: int) -> int:
    conn.execute("UPDATE users SET credits = credits + ? WHERE id = ?", (amount, user_id))
    row = conn.execute("SELECT credits FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["credits"] if row else 0
