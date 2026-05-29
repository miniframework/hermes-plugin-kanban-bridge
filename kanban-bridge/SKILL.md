---
name: kanban-bridge
description: Constraints for Hub bridge worker discovery, semantic matching, task routing, and async notifications. Activate alongside kanban-orchestrator when tasks involve remote workers or Hub routing.
version: 1.4.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [kanban, bridge, hub, remote-workers, discovery, matching]
    related_skills: [kanban-orchestrator, kanban-worker]
---

# Kanban Bridge — Discovery & Routing Constraints

## Mandatory Worker Discovery

**Step 0 (MANDATORY): discover ALL available workers before planning.**

You MUST run BOTH of these commands before creating any tasks:

1. `hermes profile list` — local profiles on this machine.
2. `hermes kanban-bridge discover` — remote workers on the Hub, with their SOUL.md descriptions. **Always run this** — if the Hub is not configured it returns an empty list harmlessly.

**CRITICAL — semantic matching, NOT name matching:**

When the user asks for "coder", "reviewer", "view", or any capability keyword, do NOT look for a profile with that exact name. Instead:

1. Run `hermes kanban-bridge discover` and read each worker's **Description** field.
2. If a worker's description mentions that capability (e.g., worker2's description says "you are coder review"), that worker IS the match.
3. **Use the `Assignee` field exactly as shown** — it already handles local vs remote:
   - `[LOCAL]` workers: assignee is just the profile name (e.g., `worker1`)
   - `[REMOTE]` workers: assignee is `hub:<email>:<profile>` (e.g., `hub:alice@example.com:worker2`)
   - **Never add `hub:` prefix to LOCAL workers** — they run on this machine and don't need Hub routing.

Example: user says "让 coder 执行 X". You run `hermes kanban-bridge discover` and see:
```
## bob@example.com:worker2
- Description: you are coder review
```
→ worker2's description says "coder" → assign to `hub:bob@example.com:worker2`. Do NOT ask the user "coder doesn't exist, which profile do you mean?" — you already found the match by reading the description.

You can also use:
- `kanban_list(assignee="<some-name>")` — sanity-check a single name.
- **Ask the user** only if NO worker's description matches the requested capability.

## Anti-Temptation Rules

- **Do not execute the work yourself.** If you find yourself "just fixing this quickly" — stop and create a task for the right specialist.
- **Never create tasks without discovering workers first.** You MUST call `hermes profile list` AND `hermes kanban-bridge discover` before any `kanban_create`. Skipping discovery leads to tasks assigned to non-existent profiles that sit in `ready` forever.
- **For any concrete task, create a Kanban task and assign it.** Every single time.
- **Split multi-lane requests before creating cards.** Extract independent workstreams first, then create one card per lane.
- **Run independent lanes in parallel.** If two cards do not need each other's output, leave them unlinked.
- **Never create dependent work as independent ready cards.** If a card must wait for another card, pass `parents=[...]` in the original `kanban_create` call.
- **If no specialist fits the available profiles, check remote worker descriptions first.** Call `hermes kanban-bridge discover` and read each worker's Description field. Only ask the user if NO worker (local or remote) matches the requested capability by description.

**STOP — have you run both `hermes profile list` AND `hermes kanban-bridge discover` already?** If not, go back and run them now. Never create tasks with guessed profile names.

## Async Task Completion Notifications

When a task finishes (completed, blocked, gave_up, crashed, timed_out), the system automatically injects a notification into your session — **you do not need to poll**.

- **Remote (Hub) tasks:** `HubSubmitter` sends `[kanban-bridge] Task <id> <status>. Summary: ...`
- **Local tasks:** `LocalTaskWatcher` sends `[kanban] Task <id> <kind>. <summary>`

**Prefer waiting for notifications over polling.** After creating tasks, report back to the user and wait. Only use `hermes kanban tail <id>` or `hermes kanban show <id>` if you need to check progress mid-flight or debug a stuck task.


## Remote Worker Auto-Matching

| Tool | Purpose |
|------|---------|
| `hermes kanban-bridge discover` | List all remote workers on the Hub with their SOUL.md descriptions |

There is no tag-based matching. You — the orchestrator LLM — call `hermes kanban-bridge discover`, read each remote worker's SOUL.md description, and decide which worker best fits each task. **Match by capability, not by name.**

When multiple remote workers could handle a task, prefer:
1. A worker whose SOUL.md description most closely matches the task domain.
2. A worker with available capacity (`active_tasks < max_concurrent`).
3. A worker on a node with lower latency or closer proximity if known.

### Assigning to remote workers

Use the `hub:<email>:<worker>` assignee convention:

```python
kanban_create(
    title="train model on GPU cluster",
    assignee="hub:bob@example.com:gpu-worker",
    body="Fine-tune the classification model on the prepared dataset.",
)
```

### Mixed local + remote task graphs

```python
t1 = kanban_create(
    title="prepare training data",
    assignee="data-eng",              # local profile
    body="Clean and split the dataset.",
)["task_id"]

t2 = kanban_create(
    title="train model",
    assignee="hub:bob@example.com:gpu-worker", # remote worker
    body="Train on prepared data from T1.",
    parents=[t1],
)["task_id"]
```

Parent/child dependencies work across local and remote boundaries.

## Tenant Inheritance

If `HERMES_TENANT` is set in your env, pass `tenant=os.environ.get("HERMES_TENANT")` on every `kanban_create` call so child tasks stay in the same namespace.
