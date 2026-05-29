"""Hub HTTP client — all calls from Hermes nodes to the central Hub."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

log = logging.getLogger(__name__)


class HubClient:
    def __init__(self, hub_url: str, hub_secret: str, self_name: str):
        self._url = hub_url.rstrip("/")
        self._secret = hub_secret
        self._self_name = self_name

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._self_name:
            h["X-Hub-Name"] = self._self_name
        if self._secret:
            h["X-Hub-Secret"] = self._secret
        return h

    def _request(self, method: str, path: str, body: Optional[dict] = None, timeout: int = 10) -> Optional[dict]:
        url = f"{self._url}{path}"
        data = json.dumps(body).encode() if body else None
        req = Request(url, data=data, headers=self._headers(), method=method)
        try:
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except (URLError, OSError) as e:
            log.warning("hub request %s %s failed: %s", method, path, e)
            return None

    def _get(self, path: str) -> Optional[Any]:
        return self._request("GET", path)

    def _post(self, path: str, body: dict) -> Optional[dict]:
        return self._request("POST", path, body)

    def _delete(self, path: str, body: dict) -> Optional[dict]:
        return self._request("DELETE", path, body)

    # -- Nodes --

    def register_node(self) -> Optional[dict]:
        return self._post("/api/nodes/register", {"name": self._self_name, "secret": self._secret})

    def heartbeat(self) -> bool:
        r = self._post("/api/nodes/heartbeat", {"name": self._self_name})
        return r is not None

    # -- Workers --

    def share_worker(self, profile_name: str, description: str = "", max_concurrent: int = 5, credits_per_task: int = 100) -> Optional[dict]:
        return self._post("/api/workers/share", {
            "node_name": self._self_name,
            "profile_name": profile_name,
            "description": description,
            "max_concurrent": max_concurrent,
            "credits_per_task": credits_per_task,
        })

    def unshare_worker(self, profile_name: str) -> Optional[dict]:
        return self._delete("/api/workers/share", {
            "node_name": self._self_name,
            "profile_name": profile_name,
        })

    def list_workers(self) -> List[dict]:
        r = self._get("/api/workers")
        return r if isinstance(r, list) else []

    # -- Tasks --

    def create_task(self, *, target_node: str, target_worker: str, title: str,
                    body: Optional[str] = None, priority: int = 0,
                    origin_task_id: Optional[str] = None) -> Optional[dict]:
        return self._post("/api/tasks", {
            "origin_node": self._self_name,
            "origin_task_id": origin_task_id,
            "target_node": target_node,
            "target_worker": target_worker,
            "title": title,
            "body": body,
            "priority": priority,
        })

    def poll_pending(self) -> List[dict]:
        r = self._get(f"/api/tasks/pending?node={self._self_name}")
        return r if isinstance(r, list) else []

    def accept_task(self, task_id: str) -> Optional[dict]:
        return self._post(f"/api/tasks/{task_id}/accept", {})

    def submit_result(self, task_id: str, *, summary: str = "", metadata: Optional[str] = None) -> bool:
        r = self._post(f"/api/tasks/{task_id}/result", {"summary": summary, "metadata": metadata})
        return r is not None

    def fail_task(self, task_id: str, *, reason: str = "") -> bool:
        r = self._post(f"/api/tasks/{task_id}/fail", {"reason": reason})
        return r is not None

    def get_results(self) -> List[dict]:
        r = self._get(f"/api/tasks/results?origin={self._self_name}")
        return r if isinstance(r, list) else []

    def ack_task(self, task_id: str) -> bool:
        r = self._post(f"/api/tasks/{task_id}/ack", {})
        return r is not None

    def list_tasks(self) -> List[dict]:
        r = self._get(f"/api/tasks?origin={self._self_name}")
        return r if isinstance(r, list) else []

    def get_task(self, task_id: str) -> Optional[dict]:
        return self._get(f"/api/tasks/{task_id}")
