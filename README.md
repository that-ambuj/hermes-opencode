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
      review:
        max_cycles: 1
      bootstrap:
        auto_on_first_spawn: true
```

If `opencode_server.url` is unreachable and `auto_spawn_server: true`, the
plugin will spawn `opencode serve` at the configured host/port on first use.
`review.max_cycles` caps how many automatic address-and-rereview rounds the
executor runs before COMMITTING. `bootstrap.auto_on_first_spawn` controls
whether the first `oc_spawn` for a project with no skill triggers an
automatic SKILL.md generation pass.

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
| `oc_output` | Return the agent's latest assistant text from the live SSE buffer (with `/message` pull fallback) |

## Agent naming

Agent ids are `<abbrev>/<task>`, max 20 chars total:

- `abbrev` is auto-derived from the project label (`dodo-payments` → `dp`,
  `dodo-backend-bookings` → `dbb`, `payments` → `pay`) or set explicitly
  via `oc_project_add(abbrev=...)`.
- `task` is caller-supplied and slugified to kebab-case.
- Collisions append `-2`, `-3`, … with the task slug trimmed to fit.

## Bootstrap

When `oc_spawn` is called for a project whose `bootstrap_skill` is unset
and `bootstrap.auto_on_first_spawn` is `true` (the default), the plugin
creates a throwaway worktree on the project's base branch, spawns a
one-shot opencode introspection session that reads the repo (`README.md`,
`package.json`, `pyproject.toml`, `Makefile`, etc.) and writes a `SKILL.md`
with an idempotent bash block, then tears the throwaway worktree down. The
generated skill is registered under `opencode-orchestrator:<abbrev>-bootstrap`
so subsequent spawns just run the bash block directly.

If the auto-gen attempt fails the spawn returns an error and the project
remains without a bootstrap skill (no partial state). You can also force
a regeneration any time via `oc_project_regenerate_bootstrap`.

## Dashboard

The dashboard tab at `/opencode-agents` opens a WebSocket against
`/api/plugins/opencode-orchestrator/events?token=...` on mount and receives
an initial `snapshot` frame plus push `agents` / `heartbeat` deltas
whenever `agents.json` or `notifications.jsonl` change on disk. If the
WebSocket errors or closes within 5s of mount the React bundle falls back
to the original 5s REST polling loop. A `ws` / `poll` transport indicator
in the header shows which channel is active. Clicking any agent row opens
a centered detail modal with the full agent record.

## Smoke test

```bash
uv run --quiet scripts/phase0_opencode_spike.py --port 4099
```

That script exercises every HTTP contract this plugin depends on. Keep it green.

## Dashboard development

The dashboard tab is a React component bundled to a single IIFE for the host
plugin loader to inject. Edit the source, not the build artifact:

```
dashboard/
├── manifest.json
├── plugin_api.py        # FastAPI router mounted at /api/plugins/opencode-orchestrator/
├── src/
│   └── index.jsx        # CANONICAL SOURCE — edit here
└── dist/
    ├── index.js         # build output (committed; hermes loads it at runtime)
    └── style.css        # plain CSS (no build step)
```

To iterate:

```bash
cd dashboard
bun install              # or `npm install` — installs esbuild as a devDep
bun run build            # compiles src/index.jsx → dist/index.js
bun run watch            # rebuild on save
```

The host injects `window.__HERMES_PLUGIN_SDK__` (React + utils) and
`window.__HERMES_PLUGINS__.register(name, Component)` at load time; auth uses
`X-Hermes-Session-Token` from `window.__HERMES_SESSION_TOKEN__`. CSS variables
come from the host theme: `--color-foreground`, `--color-muted-foreground`,
`--color-border`, `--color-card`, `--color-primary`, `--color-ring`,
`--font-mono`.

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
