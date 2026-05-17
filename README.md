# hermes-opencode

A [hermes-agent](https://github.com/NousResearch/hermes-agent) plugin that drives multiple
[opencode](https://opencode.ai) agents running in parallel git worktrees. Spawn an agent, hand it a
verbatim prompt, and the plugin runs the full **executor → reviewer → PR** cycle in the background.
Pull requests open via `gh`. Human-in-the-loop questions surface as hermes messages and route back
to the right opencode session by inference. Hourly heartbeats and state-transition events keep you
informed via your preferred channel (CLI, dashboard, or gateway DM).

```bash
hermes plugins install that-ambuj/hermes-opencode
hermes plugins enable hermes-opencode
```

## Requirements

To run this plugin you need:

|  | Requirement | Notes |
|---|---|---|
| 🐍 | **[hermes-agent](https://github.com/NousResearch/hermes-agent)** | Any modern version. Plugin loads in-process via the hermes plugin loader. |
| 🤖 | **[opencode](https://opencode.ai) binary on `PATH`** | `curl -fsSL https://opencode.ai/install \| bash` — or any of the published distributions. |
| 🔧 | **[`gh` CLI](https://cli.github.com/) authenticated** | `gh auth login` — used to open PRs and poll merge state. |
| 🌿 | **`git ≥ 2.40`** | Worktree commands. |
| 🦴 | **`httpx`, `httpx-sse`, `PyYAML`** in hermes' Python venv | Shipped with hermes-agent by default; install via `pip install -r requirements.txt` if missing. |

For the dashboard tab to render (optional), you also need to be running hermes via
`hermes dashboard`. The plugin's dashboard backend is read-only against on-disk state, so no extra
auth setup beyond hermes' own dashboard session token.

For executor/reviewer LLM calls, opencode brings its own auth (run `opencode auth login` once).
Hermes brings its own (the LLM that powers `hermes chat` itself). The plugin doesn't need any
provider credentials of its own.

## Install (alternatives)

```bash
# canonical hermes plugin install — clones the repo into ~/.hermes/plugins/hermes-opencode/
hermes plugins install that-ambuj/hermes-opencode

# local dev — symlinks this checkout into ~/.hermes/plugins/hermes-opencode/
./install.sh

# then enable
hermes plugins enable hermes-opencode
```

## Configuration (optional)

`~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - hermes-opencode
  entries:
    hermes-opencode:
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

## Slash commands

In-session commands available from any hermes CLI or gateway chat. No LLM
round-trip — they call into the plugin directly.

| Command | Purpose |
|---|---|
| `/oc` | Print help for the `/oc` slash command (lists all subcommands). |
| `/oc list` | Pretty-printed table of all tracked agents (agent_id, project, branch, phase, pr, age). |
| `/oc attach <agent_id> [--lines N]` | Print the last N lines (default 80) of an agent's accumulated transcript. |
| `/oc questions` | List every pending opencode question, with structured options surfaced inline. |

## CLI subcommand

`hermes oco …` is the same surface available from outside an active hermes
chat session — ideal for ops, automation, and cron-driven checks. Reads the
plugin's on-disk state directly (no background event loop required).

```
hermes oco list
hermes oco status oco/refunds
hermes oco attach oco/refunds --lines 200
hermes oco kill oco/refunds --force
hermes oco projects
```

`status` accepts `--json` to emit the raw agent payload. `kill` prompts for
confirmation unless `--force` is passed; it removes the worktree unless
`--keep-worktree` is also passed.

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
generated skill is registered under `hermes-opencode:<abbrev>-bootstrap`
so subsequent spawns just run the bash block directly.

If the auto-gen attempt fails the spawn returns an error and the project
remains without a bootstrap skill (no partial state). You can also force
a regeneration any time via `oc_project_regenerate_bootstrap`.

## Dashboard

The dashboard tab at `/opencode-agents` opens a WebSocket against
`/api/plugins/hermes-opencode/events?token=...` on mount and receives
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
├── plugin_api.py        # FastAPI router mounted at /api/plugins/hermes-opencode/
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

Lives under `~/.hermes/plugins/hermes-opencode/`:

- `projects.json` — registered projects
- `agents.json` — live agents
- `wt/<agent_id_fs>/` — git worktrees (one per agent; `/` in agent id encoded as `__`)
- `logs/<agent_id_fs>.jsonl` — per-agent activity log

## Status

All planned surfaces have shipped. See [CHANGELOG.md](./CHANGELOG.md) for the per-release notes.

| ✓ | Surface | Shipped in |
|---|---|---|
| ✓ | Project registry + spawn / send / status / wait / kill round trip | v0.3.0 |
| ✓ | Tool schemas correctly exposed to the LLM (description + parameters) | v0.3.1 |
| ✓ | `oc_spawn` non-blocking via `/prompt_async` (no hermes session freeze) | v0.3.2 |
| ✓ | Dashboard tab with theme-correct CSS + refresh button + React build pipeline | v0.3.3 |
| ✓ | Click-to-inspect agent detail modal + AGENTS.md developer notes | v0.3.4 |
| ✓ | Configurable review cycles · auto-bootstrap-skill generation · SSE-based `oc_output` · dashboard live-events WebSocket · clean PR titles | v0.4.0 |
| ✓ | `/oc` slash command + `hermes oco` CLI subcommand | v0.5.0, refactored to subcommand-style in v0.7.0 |
| ✓ | Graceful recovery when `gh pr create` finds an existing PR | v0.5.1 |
| ✓ | Event notifications (`pr_opened`, `done`, `failed`, `awaiting_human`, `review_started`) + heartbeat scheduler resilience | v0.6.0 |
| ✓ | Project renamed from `opencode-orchestrator` → `hermes-opencode` | v0.8.0 |

Future work lives as GitHub issues. PRs welcome.
