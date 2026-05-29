# Plan: Multi-Hub Federation — W2 Bridge Agent Dual Registration

## Context

The current kanban-bridge only supports a single Hub. The target architecture:

```
W1 → Hub1 → W2 (Bridge Agent) → Hub2 → W3
              ↓
             W4
```

**Key constraint**: W1 only knows Hub1 — it cannot see Hub2. W1 submits tasks to Hub1. W2 is a Bridge Agent registered on both Hub1 and Hub2. When W2 receives a task from Hub1, **W2 runs its own orchestrator to decompose the task**, discovers Hub2's workers, and routes sub-tasks to W3 via Hub2.

W2 is not a simple relay/proxy — it is a full Hermes agent node with orchestrator + bridge capabilities.

## Core Design

### W2's Role

W2 receives task from Hub1 → creates local kanban task → W2's agent executes it → orchestrator skill activates → discovers workers across all hubs → decomposes into sub-tasks → sub-task assignees point to Hub2 workers → HubSubmitter routes to Hub2 → Hub2 dispatches to W3.

W2's HubSubmitter needs to know which Hub to send `hub:w3@example.com:gpu-worker` to.

### Assignee Format

No 4-segment format needed. W2 discovers workers from all hubs, assignee remains `hub:<node>:<worker>`. The key is **HubSubmitter looks up which Hub a target_node is registered on** and routes automatically.

Solution: discovery builds a `node → hub_id` mapping table. HubSubmitter queries this table to select the correct Hub.

### Config Extension

```yaml
kanban:
  bridge:
    enabled: true
    poll_interval: 5
    # Multi-hub configuration
    hubs:
      hub1:
        hub_url: http://hub1:9900
        self_name: w2@example.com
        secret: secret1
      hub2:
        hub_url: http://hub2:9900
        self_name: w2@example.com
        secret: secret2
```

When no `hubs` field is present, the existing `hub_url/self_name/secret` fields construct a single `{"default": {...}}` entry — fully backward compatible.

## Files to Modify (4 files)

### 1. `plugins/kanban-bridge/__init__.py` — Multi-Hub Initialization

**`register()`**:
- Read `hubs` dict; if absent, construct `{"default": {...}}` from `hub_url`
- Create a `HubClient` per hub, stored in `_hub_clients: Dict[str, HubClient]`
- Each hub gets independent `register_node()` + `publish_workers()`
- `HubBridge` receives `hub_clients` dict (polls pending from each hub independently)
- `HubSubmitter` receives `hub_clients` dict + node→hub routing table
- `set_hub_clients(_hub_clients)` passes dict to discovery
- Heartbeat iterates over all hubs

**`_handle_slash`** `discover/workers/tasks/status` — aggregate results from all hubs.

### 2. `plugins/kanban-bridge/bridge.py` — Multi-Hub Polling

**HubBridge**:
- `__init__(hub_clients: Dict[str, HubClient], ...)`
- `_poll_and_accept()` iterates all hubs' `poll_pending()`
- Mappings include hub_id: `_hub_to_local[hub_task_id] = (hub_id, local_id)`
- `_check_local_completions()` uses the correct client per hub_id to submit results

**HubSubmitter**:
- `__init__(hub_clients: Dict[str, HubClient], default_hub_id: str, ...)`
- Maintains `_node_to_hub: Dict[str, str]` (node_name → hub_id), populated by discovery
- `_scan_and_submit()` parses assignee `hub:<node>:<worker>`:
  - Looks up `_node_to_hub[node]` → gets hub_id → uses corresponding HubClient
  - Not found → falls back to default hub
- `_poll_results()` iterates all hubs' `get_results()`

### 3. `plugins/kanban-bridge/discovery.py` — Aggregate Multi-Hub + node→hub Mapping + Loop Prevention

- `set_hub_clients(clients: Dict[str, HubClient])`
- `discover_workers()` iterates all hubs, aggregated output:
  ```
  ## alice@example.com:worker1 [REMOTE] (hub1)
  - Description: full-stack developer
  - Load: 1/5
  ```
- **Loop prevention**: Workers where `node_name == self_name` across ALL hubs are marked LOCAL. The orchestrator will never assign remote tasks back to self. All hubs must use the same `self_name`.
- **New `build_node_hub_map() -> Dict[str, str]`**: Iterates workers from all hubs, builds node_name → hub_id mapping for HubSubmitter routing.

### 4. `plugins/kanban-bridge/publisher.py` — Multi-Hub Publishing

- `publish_workers()` receives `Dict[str, HubClient]`
- Config can specify which hubs each worker publishes to (defaults to all hubs)

### 5. `plugins/kanban-bridge/__init__.py` — share/unshare Multi-Hub Support

**share/unshare/set-concurrency/set-credits** add `--hub` parameter:
```bash
hermes kanban-bridge share worker1              # Default: first (primary) hub only
hermes kanban-bridge share worker1 --hub hub2   # Specific hub
hermes kanban-bridge share worker1 --hub all    # All hubs
```

No `--hub` → operates on the first hub only (backward compatible).

**Config `publish_workers`** extension:
```yaml
publish_workers:
  - profile: worker1
    share: true
    hubs: [hub1]           # Only hub1; omit for first hub only
  - profile: gpu-worker
    share: true
    hubs: [hub1, hub2]     # Publish to multiple hubs
```

## Loop Prevention

1. **Unified self_name** — All hubs in the config use the same `self_name`. Discovery filters self globally.
2. **LOCAL tagging** — Discovery iterates all hubs; `node_name == self_name` → LOCAL. Orchestrator will never route tasks to self via hub.
3. **origin_node check** — HubBridge `_poll_and_accept()` already skips tasks where `task.origin_node == self_name` (won't accept tasks it originated).

## W2 Bridge Agent SOUL.md

W2 needs a dedicated SOUL.md that forces orchestrator activation regardless of task content keywords:

**New file: `~/.hermes/profiles/bridge-agent/SOUL.md`** (or the profile directory W2 uses):

```markdown
# Bridge Agent Profile

You are a Bridge Agent. You do NOT execute tasks yourself.

## Mandatory Behavior

For EVERY kanban task you receive:

1. Run `hermes kanban-bridge discover` to see all available workers across all Hubs.
2. Run `hermes profile list` to see local profiles.
3. Analyze the task requirements and match them to the best available worker by reading each worker's Description field.
4. Create kanban sub-tasks assigned to the matched workers.
5. Wait for results and compile a summary.

**Never attempt to do the work yourself.** Your role is decomposition and routing — always delegate to specialist workers.
```

When W2 shares on Hub1, the profile is `bridge-agent`. HubBridge creates local tasks with `assignee="bridge-agent"`, the dispatcher spawns an agent with this profile, and the SOUL.md forces discover + delegation.

## W2 Task Activation Mechanism

HubBridge receives task from Hub1 → `kanban_db.create_task(status="ready", assignee=target_worker)` → local gateway dispatcher (polls every 60s via `kanban_db.dispatch_once()`) finds the ready task → spawns agent subprocess:

```
hermes -p <assignee> --skills kanban-worker chat -q "work kanban task <id>"
```

The spawned agent reads the task content. With the Bridge Agent SOUL.md, the agent **always** runs discover first and delegates to the best-matching worker — no keyword dependency.

## Task Flow (W1 → Hub1 → W2 → Hub2 → W3)

```
1. W1: "let reviewer check the code"
2. W1's orchestrator discovers → only sees Hub1's workers
3. W1 submits to Hub1: assignee="hub:w2@example.com:bridge-agent"
4. W2's HubBridge polls Hub1 → accepts → kanban_db.create_task(ready, assignee=bridge-agent)
5. W2's dispatcher finds ready task → spawns agent subprocess with bridge-agent profile
6. W2's agent (SOUL.md: always discover first) → discovers Hub1 + Hub2 workers
7. W2 decomposes: sub-task assignee="hub:w3@example.com:reviewer"
8. W2's HubSubmitter looks up _node_to_hub["w3@example.com"] = "hub2"
9. HubSubmitter uses hub2's HubClient to create task on Hub2
10. Hub2 dispatches to W3 → W3 completes
11. W2's HubSubmitter polls Hub2 results → local task done
12. W2 completes original Hub1 task → submit_result back to Hub1
13. W1 receives result
```

## Backward Compatibility

- No `hubs` field → single hub mode, behavior unchanged
- Assignee format unchanged (`hub:<node>:<worker>`), routing decided by node→hub mapping
- Approval workflow unaffected

## Hub Server (kanban-hub)

**No changes needed.** The Hub is unaware of federation — it only sees node registration + task create/accept/complete. All routing logic lives in the W2 bridge.

## Verification

1. Single hub config — behavior unchanged (regression test)
2. Dual hub config — `discover` shows workers from both hubs
3. W2 flow — W2 receives task from Hub1, orchestrator decomposes, sub-tasks route via Hub2 to W3
4. node→hub routing — HubSubmitter auto-selects correct hub
5. Result propagation — W3 completion flows back to W1
