"""Kanban Hub — authentication middleware."""

from __future__ import annotations

from functools import wraps

from flask import request, abort


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        name = request.headers.get("X-Hub-Name", "")
        secret = request.headers.get("X-Hub-Secret", "")
        if name and secret:
            from models import connect, get_user_by_email_and_token
            with connect(readonly=True) as conn:
                if get_user_by_email_and_token(conn, email=name, token=secret):
                    return f(*args, **kwargs)
        abort(403, description="invalid or missing credentials")
    return decorated
