"""Kanban Hub — Flask application for centralized task routing and worker discovery."""

from __future__ import annotations

import time
from pathlib import Path

from functools import wraps

from flask import Flask, request, jsonify, abort, redirect, g, make_response

from auth import require_auth
from models import (
    accept_task,
    ack_task,
    add_credits,
    authenticate_user,
    complete_task,
    connect,
    create_task,
    create_user,
    fail_task,
    get_node_by_name,
    get_results_for_origin,
    get_task,
    get_user_by_token,
    heartbeat_node,
    init_db,
    list_all_tasks,
    list_nodes,
    list_pending_tasks,
    list_tasks_by_origin,
    list_workers,
    register_node,
    share_worker,
    unshare_worker,
)

import logging

log = logging.getLogger(__name__)

app = Flask(__name__)

init_db()


BLOCKED_IPS = {"10.90.34.36"}


@app.before_request
def block_banned_ips():
    if request.remote_addr in BLOCKED_IPS:
        return "", 403


@app.after_request
def log_request(response):
    log.info("%s %s %s → %d", request.remote_addr, request.method, request.path, response.status_code)
    response.headers["Connection"] = "close"
    return response

_TEMPLATE_DIR = Path(__file__).parent / "templates"


# ---------------------------------------------------------------------------
# Node endpoints
# ---------------------------------------------------------------------------

@app.route("/api/nodes/register", methods=["POST"])
@require_auth
def api_register_node():
    import hashlib
    data = request.get_json(force=True)
    name = data.get("name", "")
    sec = data.get("secret", "")
    secret_hash = hashlib.sha256(sec.encode()).hexdigest()
    with connect() as conn:
        result = register_node(conn, name=name, secret_hash=secret_hash)
    return jsonify(result)


@app.route("/api/nodes/heartbeat", methods=["POST"])
@require_auth
def api_heartbeat():
    data = request.get_json(force=True)
    name = data.get("name", "")
    with connect() as conn:
        ok = heartbeat_node(conn, name=name)
    if not ok:
        abort(404, description=f"node '{name}' not found")
    return jsonify({"status": "ok"})


@app.route("/api/nodes", methods=["GET"])
@require_auth
def api_list_nodes():
    with connect(readonly=True) as conn:
        return jsonify(list_nodes(conn))


# ---------------------------------------------------------------------------
# Worker endpoints
# ---------------------------------------------------------------------------

@app.route("/api/workers/share", methods=["POST"])
@require_auth
def api_share_worker():
    data = request.get_json(force=True)
    with connect() as conn:
        try:
            result = share_worker(
                conn,
                node_name=data.get("node_name", ""),
                profile_name=data.get("profile_name", ""),
                description=data.get("description", ""),
                max_concurrent=data.get("max_concurrent", 5),
                credits_per_task=data.get("credits_per_task", 100),
            )
        except ValueError as e:
            abort(400, description=str(e))
    return jsonify(result)


@app.route("/api/workers/share", methods=["DELETE"])
@require_auth
def api_unshare_worker():
    data = request.get_json(force=True)
    with connect() as conn:
        ok = unshare_worker(conn, node_name=data.get("node_name", ""), profile_name=data.get("profile_name", ""))
    if not ok:
        abort(404, description="worker not found")
    return jsonify({"status": "removed"})


@app.route("/api/workers", methods=["GET"])
@require_auth
def api_list_workers():
    with connect(readonly=True) as conn:
        return jsonify(list_workers(conn))


# ---------------------------------------------------------------------------
# Task endpoints
# ---------------------------------------------------------------------------

@app.route("/api/tasks", methods=["POST"])
@require_auth
def api_create_task():
    data = request.get_json(force=True)
    with connect() as conn:
        result = create_task(
            conn,
            origin_node=data.get("origin_node", ""),
            origin_task_id=data.get("origin_task_id"),
            target_node=data.get("target_node", ""),
            target_worker=data.get("target_worker", ""),
            title=data.get("title", ""),
            body=data.get("body"),
            priority=data.get("priority", 0),
        )
    return jsonify(result)


@app.route("/api/tasks/pending", methods=["GET"])
@require_auth
def api_pending_tasks():
    node = request.args.get("node", "")
    with connect(readonly=True) as conn:
        return jsonify(list_pending_tasks(conn, node=node))


@app.route("/api/tasks/results", methods=["GET"])
@require_auth
def api_get_results():
    origin = request.args.get("origin", "")
    with connect(readonly=True) as conn:
        return jsonify(get_results_for_origin(conn, origin=origin))


@app.route("/api/tasks/<task_id>/accept", methods=["POST"])
@require_auth
def api_accept_task(task_id):
    with connect() as conn:
        task = get_task(conn, task_id)
        if not task or task["status"] not in ("pending", "blocked"):
            abort(404, description="task not found or not pending")
        ok = accept_task(conn, task_id)
    if not ok:
        return jsonify({"status": "blocked", "reason": "worker at capacity"}), 200
    return jsonify({"status": "accepted"})


@app.route("/api/tasks/<task_id>/result", methods=["POST"])
@require_auth
def api_task_result(task_id):
    data = request.get_json(force=True)
    with connect() as conn:
        ok = complete_task(conn, task_id, summary=data.get("summary"), metadata=data.get("metadata"))
    if not ok:
        abort(404, description="task not found or not running")
    return jsonify({"status": "completed"})


@app.route("/api/tasks/<task_id>/fail", methods=["POST"])
@require_auth
def api_task_fail(task_id):
    data = request.get_json(force=True)
    with connect() as conn:
        ok = fail_task(conn, task_id, reason=data.get("reason", ""))
    if not ok:
        abort(404, description="task not found or not in progress")
    return jsonify({"status": "failed"})


@app.route("/api/tasks/<task_id>/ack", methods=["POST"])
@require_auth
def api_ack_task(task_id):
    with connect() as conn:
        ok = ack_task(conn, task_id)
    if not ok:
        abort(404, description="task not found or not completed/failed")
    return jsonify({"status": "acked"})


@app.route("/api/tasks/<task_id>", methods=["GET"])
@require_auth
def api_get_task(task_id):
    with connect(readonly=True) as conn:
        task = get_task(conn, task_id)
    if not task:
        abort(404, description="task not found")
    return jsonify(task)


@app.route("/api/tasks", methods=["GET"])
@require_auth
def api_list_tasks():
    origin = request.args.get("origin", "")
    with connect(readonly=True) as conn:
        return jsonify(list_tasks_by_origin(conn, origin=origin))


# ---------------------------------------------------------------------------
# User auth
# ---------------------------------------------------------------------------

def require_user(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get("hub_token")
        if not token:
            return redirect("/login")
        with connect(readonly=True) as conn:
            user = get_user_by_token(conn, token)
        if not user:
            resp = redirect("/login")
            resp.delete_cookie("hub_token")
            return resp
        g.user = user
        return f(*args, **kwargs)
    return decorated


def require_user_api(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get("hub_token")
        if not token:
            abort(401, description="not authenticated")
        with connect(readonly=True) as conn:
            user = get_user_by_token(conn, token)
        if not user:
            abort(401, description="invalid token")
        g.user = user
        return f(*args, **kwargs)
    return decorated


@app.route("/login")
def login_page():
    return (_TEMPLATE_DIR / "login.html").read_text(encoding="utf-8")


@app.route("/register")
def register_page():
    return (_TEMPLATE_DIR / "register.html").read_text(encoding="utf-8")


@app.route("/api/auth/register", methods=["POST"])
def api_register():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    if not email or not password:
        abort(400, description="email and password required")
    if len(password) < 6:
        abort(400, description="password must be at least 6 characters")
    with connect() as conn:
        try:
            user = create_user(conn, email=email, password=password)
        except ValueError as e:
            abort(409, description=str(e))
    resp = make_response(jsonify({"email": user["email"], "credits": user["credits"]}))
    resp.set_cookie("hub_token", user["token"], httponly=True, samesite="Lax", max_age=86400 * 30)
    return resp


@app.route("/api/auth/login", methods=["POST"])
def api_login():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    with connect(readonly=True) as conn:
        user = authenticate_user(conn, email=email, password=password)
    if not user:
        abort(401, description="invalid email or password")
    resp = make_response(jsonify({"email": user["email"], "credits": user["credits"]}))
    resp.set_cookie("hub_token", user["token"], httponly=True, samesite="Lax", max_age=86400 * 30)
    return resp


@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    resp = make_response(jsonify({"status": "ok"}))
    resp.delete_cookie("hub_token")
    return resp


@app.route("/api/auth/me")
@require_user_api
def api_me():
    u = g.user
    return jsonify({"email": u["email"], "credits": u["credits"], "token": u["token"]})


@app.route("/api/auth/buy-credits", methods=["POST"])
@require_user_api
def api_buy_credits():
    data = request.get_json(force=True)
    amount = int(data.get("amount", 0))
    if amount <= 0:
        abort(400, description="invalid amount")
    with connect() as conn:
        new_credits = add_credits(conn, g.user["id"], amount)
    return jsonify({"credits": new_credits})


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@app.route("/")
@require_user
def dashboard():
    return (_TEMPLATE_DIR / "dashboard.html").read_text(encoding="utf-8")


@app.route("/api/dashboard/nodes")
@require_user_api
def dashboard_nodes():
    with connect(readonly=True) as conn:
        nodes = list_nodes(conn)
        now = int(time.time())
        for n in nodes:
            if n.get("last_heartbeat") and now - n["last_heartbeat"] > 120:
                n["status"] = "offline"
        return jsonify(nodes)


@app.route("/api/dashboard/workers")
@require_user_api
def dashboard_workers():
    with connect(readonly=True) as conn:
        return jsonify(list_workers(conn))


@app.route("/api/dashboard/tasks")
@require_user_api
def dashboard_tasks():
    with connect(readonly=True) as conn:
        return jsonify(list_all_tasks(conn))


@app.route("/api/dashboard/tasks/<task_id>")
@require_user_api
def dashboard_task_detail(task_id):
    with connect(readonly=True) as conn:
        task = get_task(conn, task_id)
    if not task:
        abort(404, description="task not found")
    return jsonify(task)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    from werkzeug.serving import make_server
    from concurrent.futures import ThreadPoolExecutor
    import socketserver

    class PoolMixIn(socketserver.ThreadingMixIn):
        pool = ThreadPoolExecutor(max_workers=16)
        def process_request(self, request, client_address):
            self.pool.submit(self.process_request_thread, request, client_address)

    server = make_server("0.0.0.0", 9900, app, threaded=False)
    server.__class__ = type("PoolServer", (PoolMixIn, server.__class__), {})
    log.info("Hub starting on :9900 (thread pool = 16)")
    server.serve_forever()
