# hermes-opencode

A [hermes-agent](https://github.com/NousResearch/hermes-agent) plugin that drives multiple
[opencode](https://opencode.ai) agents running in parallel git worktrees. Spawn an agent, hand it a
verbatim prompt, and the plugin runs the full **executor → reviewer → PR** cycle in the background.
Pull requests open via `gh`. Human-in-the-loop questions and permission requests surface as
DM notifications (or hermes CLI messages) and route back to the right opencode session by inference.
An awaiting-input classifier protects the reviewer from racing into incomplete work when the
executor stops to ask for clarification. Hourly heartbeats and state-transition events keep you
informed via your preferred channel (CLI, dashboard, or gateway DM — auto-detected from your
hermes home channel).

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
| 🔧 | **[`gh` CLI](https://cli.github.com/) authenticated** | `gh auth login` — used to poll merge state. (PRs are opened by the executor itself via `gh`.) |
| 🌿 | **`git ≥ 2.40`** | Worktree commands. |
| 🦴 | **`httpx`, `httpx-sse`, `PyYAML`** in hermes' Python venv | Shipped with hermes-agent by default; install via `pip install -r requirements.txt` if missing. |

For the dashboard tab to render (optional), you also need to be running hermes via
`hermes dashboard`. The plugin's dashboard backend is read-only against on-disk state, so no extra
auth setup beyond hermes' own dashboard session token.

For executor/reviewer LLM calls, opencode brings its own auth (run `opencode auth login` once).
Hermes brings its own (the LLM that powers `hermes chat` itself). The awaiting-input classifier
uses whichever provider you've configured globally for hermes' auxiliary tasks — no separate API
key required.

## Install (alternatives)

```bash
# canonical hermes plugin install — clones the repo into ~/.hermes/plugins/hermes-opencode/
hermes plugins install that-ambuj/hermes-opencode

# local dev — symlinks this checkout into ~/.hermes/plugins/hermes-opencode/
./install.sh

# then enable
hermes plugins enable hermes-opencode
```

## Configuration

All knobs live under `plugins.entries.hermes-opencode` in `~/.hermes/config.yaml`. Every section is
optional — the defaults below match what the plugin uses out of the box.

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
      notify:
        # sinks defaults to ["gateway","dashboard"] when a <PLATFORM>_HOME_CHANNEL
        # env var is detected (bluebubbles, telegram, discord, slack, teams, ...);
        # otherwise to ["cli","dashboard"]. Explicit value here always wins.
        sinks: ["gateway", "dashboard"]
        gateway:
          platform: bluebubbles
          chat_id: "+15551234567"   # optional if BLUEBUBBLES_HOME_CHANNEL is set
        events:
          enabled: ["pr_opened", "done", "failed", "awaiting_human", "review_started", "cancelled"]
      heartbeat:
        enabled: true
        unconditional_hours: [9, 23]
        timezone: "Asia/Calcutta"
      classifier:
        enabled: true
        task: hermes_opencode.awaiting_input
        max_input_chars: 2000
        max_output_tokens: 80
        timeout_sec: 8
      awaiting_input:
        stall_timeout_sec: 300
        reminder_interval_sec: 1800
```

If `opencode_server.url` is unreachable and `auto_spawn_server: true`, the plugin spawns
`opencode serve` at the configured host/port on first use; a background watchdog restarts it if
it dies. `review.max_cycles` caps how many automatic address-and-rereview rounds the executor
runs before COMMITTING. `bootstrap.auto_on_first_spawn` controls whether the first `oc_spawn`
for a project with no skill triggers an automatic SKILL.md generation pass.

### Notifications

| Sink | Behavior |
|---|---|
| `gateway` | Sends a DM to your configured channel via hermes-agent's gateway adapter. Works for BlueBubbles / iMessage, Telegram, Discord, Slack, Teams, Google Chat, Feishu, WeCom, Line, IRC, Mattermost, SMS, QQ. Auto-detected from `<PLATFORM>_HOME_CHANNEL` env vars when no explicit `notify.gateway` block is set. |
| `dashboard` | Appends a record to `notifications.jsonl`; the dashboard tab and live WebSocket pick it up. |
| `cli` | Injects an in-session hermes message. Useful when you're driving hermes interactively. |

Events that fire notifications: `pr_opened`, `done`, `failed`, `awaiting_human`, `review_started`,
`cancelled`. Disable any subset by reducing `notify.events.enabled`. The `serve_down` event
(opencode-server crash) always fires to `cli`, `dashboard`, and `gateway` regardless of config.

### Awaiting-input classifier

When the executor goes idle with worktree changes but with no formal `/question` or `/permission`
entry, the plugin can't tell from opencode's message protocol whether the executor finished or is
waiting on a plain-text "which option do you prefer?" prompt. A three-layer cascade gates the
`EXECUTING → IDLE_TASK_COMPLETE → REVIEW_SPAWNING` transition to avoid running the reviewer against
incomplete work:

1. **Regex layer** (always on, free): 10 patterns covering trailing `?`, which-option phrasing,
   should-I, would-you-prefer, let-me-know, please-confirm, y/n, awaiting-your-input, and labeled
   options ("Option A:", "Option B:").
2. **LLM layer** (configurable): calls hermes-agent's auxiliary client
   (`agent.auxiliary_client.async_call_llm`) with task
   `hermes_opencode.awaiting_input`. Pick your model by adding a task-level override to your
   global hermes config:

   ```yaml
   auxiliary:
     hermes_opencode.awaiting_input:
       provider: anthropic
       model: claude-haiku-4-5
   ```

   Works with Anthropic, OpenAI, Gemini, OpenRouter, or any provider hermes routes. Falls back
   gracefully to the regex result on import/network/parse/timeout errors. Set
   `classifier.enabled: false` to run regex-only.
3. **Stalled-idle reminder loop**: re-notifies `awaiting_human` for any agent that's been
   waiting longer than `awaiting_input.reminder_interval_sec` (default 30 min).

When the cascade detects awaiting, the agent stays in `EXECUTING` and a context-rich DM goes out
with the last assistant text snippet. The reviewer is never run against incomplete work.

The executor is also instructed to use the `/question` API as the authoritative signal — the
classifier is the safety net for noncompliance, not the primary path.

## Tools

All 21 tools are exposed to the LLM under the `hermes_opencode` toolset. Names map 1:1 to their
underlying handler in [`tools.py`](./tools.py).

| Tool | Purpose |
|---|---|
| `oc_project_add` | Register a project (label, repo path, base branch, optional abbrev, optional bootstrap_skill) |
| `oc_project_list` | List registered projects |
| `oc_project_show` | Show one project's full config |
| `oc_project_remove` | Unregister a project |
| `oc_project_set_repo_path` | Update the local repo path for a project |
| `oc_project_regenerate_bootstrap` | Re-run the introspection that produces the project's `bootstrap` skill (and `cleanup` skill) |
| `oc_project_regenerate_cleanup` | Re-run only the cleanup-skill introspection (backward-compat for projects that have a bootstrap but no cleanup yet) |
| `oc_spawn` | Create worktree + opencode session, send initial prompt verbatim (wrapped in a HERMES-OPENCODE system directive) |
| `oc_send` | Send a message to a live agent |
| `oc_status` | Show one agent or all agents |
| `oc_wait` | Block until an agent goes idle |
| `oc_kill` | Abort agent's session; record erased from `agents.json` |
| `oc_cancel` | Wind down an agent without merging; record kept as `CANCELLED` with a reason |
| `oc_answer` | Forward the user's verbatim answer to a pending opencode `/question` or `/permission` |
| `oc_output` | Return the agent's latest assistant text from the live SSE buffer (with `/message` pull fallback) |
| `oc_review_now` | Trigger the reviewer immediately (skip the idle-debounce + awaiting-input gate) |
| `oc_review_again` | Re-run the reviewer on the executor's current state |
| `oc_skip_review` | Bypass review and go straight to COMMITTING / PR open |
| `oc_pr_status` | Poll GitHub for the PR's current state |
| `oc_set_notify_target` | Update `notify.gateway.{platform,chat_id}` at runtime without restarting hermes |
| `oc_heartbeat_send_now` | Force the hourly heartbeat report to fire immediately |

## Slash commands

In-session commands available from any hermes CLI or gateway chat (iMessage, Telegram, Discord,
Slack, …). No LLM round-trip — they call into the plugin directly via the
`pre_gateway_dispatch` hook.

| Command | Purpose |
|---|---|
| `/oc` | Print help for the `/oc` slash command (lists all subcommands). |
| `/oc list [--archived]` | One-line-per-agent status (phase glyph + age + PR URL; indented continuation for failures, cancellations, merged PRs). `--archived` includes the 12h-archived rows. |
| `/oc attach <agent_id> [--lines N]` | Print the last N lines (default 80) of an agent's accumulated transcript. |
| `/oc questions` | List every pending opencode question, with structured options surfaced inline. |
| `/oc cancel <agent_id> [reason ...]` | Wind down an agent without merging; runs the full cleanup sequence (sessions, sister worktree, cleanup skill, executor worktree) and keeps the record as `CANCELLED`. |
| `/oc doctor` | Single-message plugin health report (versions, bg loop, deps, state files, notify config, classifier config, per-agent awaiting-input verdicts, last events). Paste into bug reports. |
| `/oc test-notify [message ...]` | Force a full notify fanout (gateway DM + dashboard + cli) with a synthetic event; prints per-sink `[ok]` / `[FAIL]` with detail. For verifying the gateway DM trigger end-to-end. |

## CLI subcommand

`hermes oco …` is the same surface available from outside an active hermes chat session — ideal
for ops, automation, and cron-driven checks. Reads the plugin's on-disk state directly (no
background event loop required).

```
hermes oco list [--archived]
hermes oco status oco/refunds [--json]
hermes oco attach oco/refunds --lines 200
hermes oco kill oco/refunds --force [--keep-worktree]
hermes oco cancel oco/refunds [reason ...]
hermes oco projects
```

`kill` removes the worktree by default; pass `--keep-worktree` to retain it.
`cancel` runs the same teardown as `/oc cancel` but keeps the record as `CANCELLED` (audit).

## Agent naming

Agent ids are `<abbrev>/<task>`, max 20 chars total:

- `abbrev` is auto-derived from the project label (`dodo-payments` → `dp`,
  `dodo-backend-bookings` → `dbb`, `payments` → `pay`) or set explicitly
  via `oc_project_add(abbrev=...)`.
- `task` is caller-supplied and slugified to kebab-case.
- Collisions append `-2`, `-3`, … with the task slug trimmed to fit.

## State machine

```
CREATED → BOOTSTRAPPING → EXECUTING → IDLE_TASK_COMPLETE →
REVIEW_SPAWNING → REVIEWING → REVIEW_DELIVERED → EXECUTOR_ADDRESSING →
IDLE_REVIEW_ADDRESSED → COMMITTING → PR_OPEN → DONE
```

Terminal: `DONE` (merged, archived after 12 h), `CANCELLED` (manual / auto on PR closed, archived
after 12 h), `KILLED` (hard-deleted), `FAILED` (kept indefinitely until manually killed).

`EXECUTING → IDLE_TASK_COMPLETE` requires all of: opencode reports the session idle, no pending
`/question` or `/permission`, worktree has uncommitted or unpushed changes, 30s stable idleness,
and the awaiting-input cascade does not flag the last assistant text as a question awaiting human
reply.

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
remains without a bootstrap skill (no partial state). You can force a
regeneration any time via `oc_project_regenerate_bootstrap`.

The same introspection session also emits a matching **cleanup skill** at
`~/.hermes/skills/hermes-opencode:<abbrev>-cleanup/SKILL.md` that inverses
the bootstrap's side effects (stop docker-compose services, drop
ephemeral databases, remove generated `.env` files, etc.). It runs
automatically before the worktree is removed — both when the PR merges
(DONE transition) and when you call `oc_kill --remove-worktree` or
`oc_cancel`. Cleanup failures are logged but don't block teardown.
Projects whose bootstrap was authored before cleanup-skill generation
existed can backfill via `oc_project_regenerate_cleanup`.

## Dashboard

The dashboard tab at `/opencode-agents` opens a WebSocket against
`/api/plugins/hermes-opencode/events?token=...` on mount and receives an
initial `snapshot` frame plus push `agents` / `heartbeat` deltas whenever
`agents.json` or `notifications.jsonl` change on disk. Archived agents
are hidden by default; toggle the **show archived** checkbox or pass
`?include_archived=1` on the REST endpoint to include them. If the
WebSocket errors or closes within 5 s of mount the React bundle falls
back to the original 5 s REST polling loop. A `ws` / `poll` transport
indicator in the header shows which channel is active. Clicking any
agent row opens a centered detail modal with the full agent record.

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
- `agents.json` — live + archived agents (archived rows survive for audit; the pruner doesn't hard-delete)
- `notifications.jsonl` — dashboard event feed (append-only, rotated at 1000 lines)
- `events.log` — structured event log (append-only, rotated at 5000 lines)
- `history.jsonl` — archive of DONE / CANCELLED transitions (30-day retention)
- `wt/<agent_id_fs>/` — git worktrees (one per agent; `/` in agent id encoded as `__`)
- `wt/<agent_id_fs>.review/` — reviewer's sister worktree (auto-staged + torn down)
- `logs/<agent_id_fs>.jsonl` — per-agent activity log

## Coexistence with oh-my-openagent

If you run [oh-my-openagent](https://github.com/code-yeongyu/oh-my-opencode) (OMO) as an opencode
plugin (registered globally in `~/.config/opencode/opencode.json`), it injects directives into
the executor with the prefix `[SYSTEM DIRECTIVE: OH-MY-OPENCODE - <TYPE>]`. This plugin uses the
parallel prefix `[SYSTEM DIRECTIVE: HERMES-OPENCODE - <TYPE>]` for its own directives so the two
coexist cleanly — OMO's parser checks for the OH-MY-OPENCODE marker specifically and ignores ours.

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
| ✓ | Project renamed `opencode-orchestrator` → `hermes-opencode` | v0.8.0 |
| ✓ | Gateway slash dispatch: `/oc` works from iMessage / Telegram / Discord / Slack | v0.9.0 |
| ✓ | Executor-driven PR open · archived-not-deleted agents · executor commits under user git identity · clean reviewer prompts | v0.10.0 |
| ✓ | `oc_project_regenerate_cleanup` tool + `--archived` flag on listing surfaces | v0.11.0 |
| ✓ | `CANCELLED` phase + `oc_cancel` tool/slash/CLI · auto-cancel on PR closed · auto-detected DM channel · opencode-serve watchdog | v0.12.0 / v0.12.1 |
| ✓ | Gateway notify sink fix (live-runner adapter resolution) + `/oc test-notify` diagnostic | v0.13.0 |
| ✓ | Awaiting-input classifier cascade + reviewer-eagerness gate + permission-notify · `[SYSTEM DIRECTIVE: HERMES-OPENCODE]` initial-prompt envelope | v0.14.0 |

Future work lives as GitHub issues. PRs welcome.
