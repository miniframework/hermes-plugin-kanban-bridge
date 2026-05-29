"""kanban-bridge plugin — hub mode: centralized task routing via Hub server.

Config (config.yaml):
  kanban:
    bridge:
      enabled: true
      mode: hub
      hub_url: http://hub-server:8800
      hub_secret: shared-secret
      self_name: email
      poll_interval: 5
      publish_workers:
        - profile: backend-dev
          share: true
        - profile: gpu-worker
          share: false
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict

log = logging.getLogger(__name__)

_hub_client = None
_bridge = None
_submitter = None
_local_watcher = None
_heartbeat_thread = None
_config: Dict[str, Any] = {}


def get_bridge():
    """Return the HubBridge instance, for use by discord.py approval interceptor."""
    return _bridge


def _load_bridge_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        return cfg.get("kanban", {}).get("bridge", {})
    except Exception:
        return {}


def _start_heartbeat(interval: int = 60):
    global _heartbeat_thread
    import time

    def _beat():
        while True:
            try:
                if _hub_client:
                    _hub_client.heartbeat()
            except Exception:
                log.debug("heartbeat failed")
            time.sleep(interval)

    _heartbeat_thread = threading.Thread(target=_beat, daemon=True, name="kanban-hub-heartbeat")
    _heartbeat_thread.start()


def _handle_slash(args: str, **kwargs) -> str:
    parts = args.strip().split() if args else []
    cmd = parts[0] if parts else "status"

    if cmd == "status":
        lines = [
            f"Hub mode: {_config.get('self_name', 'unknown')}",
            f"Hub URL: {_config.get('hub_url', 'not set')}",
        ]
        if _bridge:
            lines.append(f"Inbound tracked: {_bridge.tracked_count}")
        if _submitter:
            lines.append(f"Outbound tracked: {_submitter.tracked_count}")
        return "\n".join(lines)

    if cmd == "discover":
        from .discovery import discover_workers
        return discover_workers()

    if cmd == "share":
        profile = parts[1] if len(parts) > 1 else ""
        if not profile:
            return "Usage: /kanban-bridge share <profile_name>"
        if not _hub_client:
            return "Hub not configured."
        from .publisher import _read_profile_description
        desc = _read_profile_description(profile)
        r = _hub_client.share_worker(profile, description=desc)
        return f"Shared: {r}" if r else "Failed to share."

    if cmd == "unshare":
        profile = parts[1] if len(parts) > 1 else ""
        if not profile:
            return "Usage: /kanban-bridge unshare <profile_name>"
        if not _hub_client:
            return "Hub not configured."
        r = _hub_client.unshare_worker(profile)
        return f"Unshared: {r}" if r else "Failed to unshare (not found?)."

    if cmd == "workers":
        if not _hub_client:
            return "Hub not configured."
        workers = _hub_client.list_workers()
        if not workers:
            return "No workers on Hub."
        lines = []
        for w in workers:
            lines.append(f"  {w.get('node_name')}:{w.get('profile_name')} "
                         f"({w.get('active_tasks', 0)}/{w.get('max_concurrent', 1)})")
        return "\n".join(lines)

    if cmd == "tasks":
        if not _hub_client:
            return "Hub not configured."
        tasks = _hub_client.list_tasks()
        if not tasks:
            return "No tasks on Hub for this node."
        lines = []
        for t in tasks:
            lines.append(f"  {t.get('id')}  {t.get('status'):10s}  {t.get('target_node')}:{t.get('target_worker')}  {t.get('title')}")
        return "\n".join(lines)

    if cmd == "set-concurrency":
        if len(parts) < 3:
            return "Usage: /kanban-bridge set-concurrency <profile> <max_concurrent>"
        profile = parts[1]
        try:
            max_conc = int(parts[2])
        except ValueError:
            return "max_concurrent must be an integer."
        if max_conc < 1:
            return "max_concurrent must be >= 1."
        if not _hub_client:
            return "Hub not configured."
        from .publisher import _read_profile_description
        desc = _read_profile_description(profile)
        r = _hub_client.share_worker(profile, description=desc, max_concurrent=max_conc)
        return f"Updated {profile} max_concurrent={max_conc}: {r}" if r else "Failed to update."

    if cmd == "set-credits":
        if len(parts) < 3:
            return "Usage: /kanban-bridge set-credits <profile> <credits_per_task>"
        profile = parts[1]
        try:
            credits = int(parts[2])
        except ValueError:
            return "credits_per_task must be an integer."
        if credits < 0:
            return "credits_per_task must be >= 0."
        if not _hub_client:
            return "Hub not configured."
        from .publisher import _read_profile_description
        desc = _read_profile_description(profile)
        r = _hub_client.share_worker(profile, description=desc, credits_per_task=credits)
        return f"Updated {profile} credits_per_task={credits}: {r}" if r else "Failed to update."

    if cmd in ("approve", "reject"):
        hub_id = parts[1] if len(parts) > 1 else ""
        if not hub_id:
            return f"Usage: /kanban-bridge {cmd} <hub_id>"
        if not _bridge:
            return "Bridge not running."
        decision = "approved" if cmd == "approve" else "rejected"
        ok = _bridge.resolve_approval(hub_id, decision)
        return f"{'✅ Approved' if cmd == 'approve' else '❌ Rejected'} {hub_id}" if ok else f"{hub_id} not found in pending approvals"

    return f"Unknown: {cmd}. Available: status, discover, share, unshare, workers, tasks, set-concurrency, set-credits, approve, reject"


def register(ctx) -> None:
    global _hub_client, _bridge, _submitter, _local_watcher, _config

    if _bridge:
        _bridge.stop()
    if _submitter:
        _submitter.stop()
    if _local_watcher:
        _local_watcher.stop()

    _config = _load_bridge_config()
    if not _config.get("enabled", False):
        log.debug("kanban-bridge: disabled")
        return

    from .hub_client import HubClient
    from .bridge import HubBridge, HubSubmitter, LocalTaskWatcher
    from .discovery import set_hub_client
    from .publisher import publish_workers

    hub_url = _config.get("hub_url", "")
    hub_secret = _config.get("hub_secret", "") or _config.get("secret", "")
    self_name = _config.get("self_name", "unknown")
    poll_interval = _config.get("poll_interval", 5)

    if not hub_url:
        log.warning("kanban-bridge: hub_url not set, skipping")
        return

    _hub_client = HubClient(hub_url, hub_secret, self_name)
    set_hub_client(_hub_client)

    r = _hub_client.register_node()
    if r:
        log.info("kanban-bridge: registered as %s (%s)", self_name, r.get("action", "?"))

    pub_config = _config.get("publish_workers", [])
    if pub_config:
        publish_workers(_hub_client, pub_config)

    _bridge = HubBridge(
        _hub_client,
        poll_interval=poll_interval,
        self_name=self_name,
        approval_required=_config.get("approval_required", False),
        approval_channel=str(_config.get("approval_channel", "")),
    )
    _bridge.start()

    _submitter = HubSubmitter(_hub_client, poll_interval=poll_interval, plugin_ctx=ctx)
    _submitter.start()

    _local_watcher = LocalTaskWatcher(plugin_ctx=ctx, poll_interval=poll_interval)
    _local_watcher.set_hub_task_ids(_bridge._local_to_hub, _submitter._submitted)
    _local_watcher.start()

    _start_heartbeat()

    ctx.register_command(
        "kanban-bridge",
        handler=_handle_slash,
        description="Hub-mode kanban bridge: discover, share, unshare, status",
        args_hint="[status|discover|share|unshare|workers|tasks]",
    )

    _KANBAN_DISCOVER_SCHEMA = {
        "name": "kanban_discover",
        "description": (
            "List all remote workers available on the Hub with their SOUL.md descriptions. "
            "Use this to find workers by capability — match the user's request against each "
            "worker's description field, not the profile name. For example, if the user asks "
            "for 'coder' and a worker's description says 'you are coder review', that worker "
            "is the match. Assign using hub:<email>:<profile> format."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    }

    def _handle_discover(args: dict, **kw) -> str:
        from .discovery import discover_workers
        return discover_workers()

    ctx.register_tool(
        name="kanban_discover",
        toolset="core",
        schema=_KANBAN_DISCOVER_SCHEMA,
        handler=_handle_discover,
        emoji="🔍",
    )

    # -- CLI subcommand: hermes kanban-bridge ... ----------------------------

    def _setup_cli(parser):
        sub = parser.add_subparsers(dest="bridge_cmd")
        sub.add_parser("status", help="Show bridge status")
        sub.add_parser("discover", help="Discover remote workers")
        sub.add_parser("workers", help="List workers on Hub")
        sub.add_parser("tasks", help="List tasks on Hub")

        sp_share = sub.add_parser("share", help="Share a worker profile")
        sp_share.add_argument("profile", help="Profile name to share")

        sp_unshare = sub.add_parser("unshare", help="Unshare a worker profile")
        sp_unshare.add_argument("profile", help="Profile name to unshare")

        sp_conc = sub.add_parser("set-concurrency", help="Set max_concurrent for a worker")
        sp_conc.add_argument("profile", help="Profile name")
        sp_conc.add_argument("max_concurrent", type=int, help="Max concurrent tasks")

        sp_cred = sub.add_parser("set-credits", help="Set credits_per_task for a worker")
        sp_cred.add_argument("profile", help="Profile name")
        sp_cred.add_argument("credits_per_task", type=int, help="Credits per task")

    def _handle_cli(args):
        cmd = getattr(args, "bridge_cmd", None) or "status"
        raw = cmd
        if cmd in ("share", "unshare", "set-concurrency", "set-credits"):
            raw = f"{cmd} {args.profile}"
            if cmd == "set-concurrency":
                raw += f" {args.max_concurrent}"
            elif cmd == "set-credits":
                raw += f" {args.credits_per_task}"
        print(_handle_slash(raw))

    ctx.register_cli_command(
        name="kanban-bridge",
        help="Hub-mode kanban bridge management",
        setup_fn=_setup_cli,
        handler_fn=_handle_cli,
        description="Manage kanban-bridge: status, discover, share, unshare, workers, tasks, set-concurrency, set-credits",
    )

    log.info("kanban-bridge registered (hub=%s, self=%s)", hub_url, self_name)
