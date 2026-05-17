# opencode-orchestrator

A hermes-agent plugin that drives multiple opencode agents running in git worktrees.

Status: **Phase 1** — project registry + spawn / send / status / wait / kill round trip.
Phases 2–4 add the executor/reviewer cycle with PR automation, hourly heartbeat DMs,
project bootstrap (with opencode-driven recovery), and a dashboard tab.

## Install

```bash
# Recommended (canonical hermes path): clones repo into ~/.hermes/plugins/opencode-orchestrator/
hermes plugins install <owner>/opencode-orchestrator

# Local development: symlinks this checkout into ~/.hermes/plugins/opencode-orchestrator/
./install.sh
```

Then enable it:

```bash
hermes plugins enable opencode-orchestrator
```

## Requirements

- `opencode` binary on `PATH`
- `httpx`, `httpx-sse`, `PyYAML` available in hermes' Python environment

## Configuration (optional)

`~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - opencode-orchestrator
  entries:
    opencode-orchestrator:
      opencode_server:
        url: "http://127.0.0.1:4096"
        password: "${OPENCODE_SERVER_PASSWORD}"
      pr:
        base_branch: main
      auto_spawn_server: true
```

If `opencode_server.url` is unreachable and `auto_spawn_server: true`, the
plugin will spawn `opencode serve` at the configured host/port on first use.

## Phase 1 tools

| Tool | Purpose |
|---|---|
| `oc_project_add` | Register a project (label, repo path, base branch, optional abbrev) |
| `oc_project_list` | List registered projects |
| `oc_project_show` | Show one project's full config |
| `oc_project_remove` | Unregister a project |
| `oc_project_set_repo_path` | Update the local repo path for a project |
| `oc_spawn` | Create worktree + opencode session, send initial prompt VERBATIM |
| `oc_send` | Send a message to a live agent |
| `oc_status` | Show one agent or all agents |
| `oc_wait` | Block until an agent goes idle |
| `oc_kill` | Abort agent's session, optionally prune worktree |

## Agent naming

Agent ids are `<abbrev>/<task>`, max 20 chars total:

- `abbrev` is auto-derived from the project label (`dodo-payments` → `dp`,
  `dodo-backend-bookings` → `dbb`, `payments` → `pay`) or set explicitly
  via `oc_project_add(abbrev=...)`.
- `task` is caller-supplied and slugified to kebab-case.
- Collisions append `-2`, `-3`, … with the task slug trimmed to fit.

## Smoke test

```bash
uv run --quiet scripts/phase0_opencode_spike.py --port 4099
```

That script exercises every HTTP contract this plugin depends on. Keep it green.

## State

Lives under `~/.hermes/plugins/opencode-orchestrator/`:

- `projects.json` — registered projects
- `agents.json` — live agents
- `wt/<agent_id_fs>/` — git worktrees (one per agent; `/` in agent id encoded as `__`)
- `logs/<agent_id_fs>.jsonl` — per-agent activity log

## Roadmap

| Phase | Surface |
|---|---|
| 1 ✓ | Project registry + spawn/send/status/wait/kill |
| 2 | SSE consumer · executor/reviewer cycle · PR open · pre-LLM-call question routing · `oc_answer`/`oc_review_*` tools |
| 2.5 | ProjectBootstrap (shell + opencode-driven recovery + skill generation) |
| 3 | Heartbeat DMs · CLI/gateway/dashboard sinks · 4h done retention |
| 4 | FastAPI + React dashboard tab |
