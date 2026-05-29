# Kanban Bridge Profile

## Kanban Trigger Rule

When the user's request mentions **any** of these keywords or intents, **always** activate both `kanban-orchestrator` and `kanban-bridge` together:

- worker, agent, profile, delegate, assign, route
- kanban, task, subtask, fan-out, parallel
- hub, remote, bridge, cross-node
- "let X do it", "ask X to", "send to X"

**Do not use one without the other.** They are a pair:
- **kanban-orchestrator** — decomposes the request, plans the task graph, creates cards, manages dependencies
- **kanban-bridge** — discovers all workers (local + remote), does semantic matching, enforces routing constraints

## Mandatory First Step

Before any task creation, **always** run:
1. `hermes profile list`
2. `hermes kanban-bridge discover`

Match workers by **Description**, not by name. Use the exact `Assignee` field from discover output. Never guess profile names.

## Routing

- `[LOCAL]` workers → assignee is the profile name (e.g., `worker1`)
- `[REMOTE]` workers → assignee is `hub:<email>:<profile>` (e.g., `hub:bob@example.com:worker2`)
- Never add `hub:` prefix to LOCAL workers.

## After Task Creation

Do not poll. Wait for async notifications (`[kanban-bridge]` / `[kanban]`).
