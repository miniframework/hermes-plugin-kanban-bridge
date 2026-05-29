"""Discovery — fetch remote workers from Hub for LLM-driven matching."""

from __future__ import annotations

import logging
from typing import List, Optional

log = logging.getLogger(__name__)

_hub_client = None


def set_hub_client(client):
    global _hub_client
    _hub_client = client


def discover_workers() -> str:
    """Fetch all shared workers from Hub, return formatted text for LLM consumption."""
    if not _hub_client:
        return "Hub not configured — no remote workers available."

    workers = _hub_client.list_workers()
    if not workers:
        return "No remote workers available on Hub."

    self_name = _hub_client._self_name if _hub_client else ""

    lines = [
        "# Available Workers (via Hub)",
        "",
        "| Email | Profile | Type | Assignee | Capacity | Credits | Description |",
        "|------|---------|------|----------|----------|---------|-------------|",
    ]
    for w in workers:
        node = w.get("node_name", "?")
        profile = w.get("profile_name", "?")
        desc = w.get("description", "").strip().replace("\n", " ")
        max_c = w.get("max_concurrent", 1)
        active = w.get("active_tasks", 0)
        credits = w.get("credits_per_task", 100)
        is_local = (node == self_name)

        if is_local:
            assignee = profile
            tag = "LOCAL"
        else:
            assignee = f"hub:{node}:{profile}"
            tag = "REMOTE"

        lines.append(f"| {node} | {profile} | {tag} | `{assignee}` | {active}/{max_c} | {credits} | {desc} |")

    return "\n".join(lines)
