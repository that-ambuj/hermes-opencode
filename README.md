# hermes-opencode

[![ci](https://github.com/that-ambuj/hermes-opencode/actions/workflows/ci.yml/badge.svg)](https://github.com/that-ambuj/hermes-opencode/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE)
[![python 3.10+](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue.svg)](#requirements)

> Orchestrate multiple [opencode](https://opencode.ai) agents working in parallel git worktrees, driven from your hermes chat.

A [hermes-agent](https://github.com/NousResearch/hermes-agent) plugin. You say "build me X" in
hermes chat (CLI, dashboard, iMessage, Telegram, Discord, Slack, ...) and the plugin spawns an
opencode agent in an isolated worktree, runs the full **executor -> reviewer -> commit -> PR**
loop in the background, and surfaces progress + human-in-the-loop questions through your home
channel. Multiple agents run concurrently against multiple projects with no cross-contamination.

```bash
hermes plugins install that-ambuj/hermes-opencode
hermes plugins enable hermes-opencode
```

## Why

- **Real concurrency.** Each agent runs in its own git worktree under
  `~/.hermes/plugins/hermes-opencode/wt/`. Five agents working on five branches don't see each
  other's changes.
- **Hands-off PR flow.** Reviewer + executor + PR opening happen on a background asyncio loop.
  You only get pinged on `pr_opened`, `awaiting_human`, `done`, or `failed`.
- **Adaptive failure recovery.** Per-phase retry budgets, a non-terminal `NEEDS_INTERVENTION`
  phase for operator-recoverable failures, and a one-tool `oc_retry` to resume anything stuck.
- **Chat-native, not CLI-native.** Hermes sees every active agent's phase, latest output
  snippet, and recent events on every turn, so it dispatches obsessively and narrates progress
  without you asking.
- **Crash forensics.** When `opencode serve` dies, the orchestrator captures exit code, signal
  name (SIGKILL / SIGTERM / ...), uptime, and the dying process's log tail into
  `serve_crashes.jsonl`.

## Quick start

After `hermes plugins enable hermes-opencode`, register a project and dispatch your first task:

```text
> @hermes register the backend repo at ~/work/backend, base branch=main

> @hermes implement a GET /healthz endpoint on the backend repo

[hermes-opencode] -> oc_spawn(project="backend", task="healthz",
                              prompt="implement a GET /healthz endpoint")
spawned BCK/healthz -> EXECUTING (session=...)

# ~10 minutes later you get a DM:
🔗 BCK/healthz pr_opened: https://github.com/.../pull/42
```

While the agent works, ask anything else and hermes uses the active-agents context to answer
without polling:

```text
> @hermes what's it doing?

BCK/healthz is in REVIEWING (session=busy, in_phase_for=2m). Latest text:
"Running the test suite to confirm the endpoint doesn't break ..."
```

Need to intervene? Just send a follow-up — hermes recognizes the agent reference:

```text
> @hermes also add a /readyz endpoint to BCK/healthz

[hermes-opencode] -> @BCK/healthz also add a /readyz endpoint
```

Or address it directly from any channel, bypassing the chat LLM entirely:

```text
> @BCK/healthz the test you wrote is flaky, please fix

[hermes-opencode] -> @BCK/healthz
```

## What you get

### Multi-agent orchestration
Each agent owns one worktree + one opencode session + one reviewer sister-worktree. The reviewer
runs against a `git worktree add --detach` clone (opencode's `InstanceStore` is dir-keyed and
concurrent writes to the same path corrupt state, so the sister tree is non-negotiable).

### Adaptive failure recovery
Recoverable errors run a per-phase retry budget before escalating. Failures the orchestrator
can't resolve on its own (e.g. PR-fallback exhausted because `gh auth` is broken) route to
`NEEDS_INTERVENTION` instead of `FAILED`; operators fix the root cause and run `oc_retry` to
resume from the saved prior phase. A phase-stuck watchdog fires when a transitional phase has
held for >10 min.

### Proactive chat UX
The plugin injects a five-block context into every chat turn via `pre_llm_call`: a dispatcher
directive, active-agents snapshot (phase + session status + latest output snippet), recent
events since your last message, a dispatch nudge when your message reads like a task, and an
answer nudge when your reply matches a pending `/question` option. Result: hermes calls
`oc_spawn` / `oc_send` / `oc_answer` on the first try instead of asking you what to do.

Optional periodic "still working" DMs can be enabled via
`plugins.entries.hermes-opencode.progress_narration.enabled: true`.

### Awaiting-input classifier
opencode's message protocol has no field that says "asked a question". When the executor stops
with worktree changes but no formal `/question`, a four-layer cascade (regex -> todo-list
override -> LLM classifier -> stalled-idle reminder) decides whether to advance to review or
park the agent in `AWAITING_HUMAN`. The reviewer is never run against incomplete work.

### Multi-channel notifications
Events fan out to any subset of `cli` / `dashboard` / `gateway`. The gateway adapter works for
BlueBubbles / iMessage, Telegram, Discord, Slack, Teams, Google Chat, Feishu, WeCom, Line, IRC,
Mattermost, SMS, and QQ. The DM target auto-detects from `<PLATFORM>_HOME_CHANNEL` env vars when
not explicitly configured.

### Read-only dashboard
A React tab at `/opencode-agents` connects via WebSocket to receive snapshot + delta frames
whenever `agents.json` or `notifications.jsonl` change. Falls back to 5-second REST polling if
the WebSocket can't connect. Click any agent row for a centered detail modal.

### Crash post-mortem
Every detected `opencode serve` death appends a structured row to `serve_crashes.jsonl`: exit
code, signal name, uptime, log tail (last 20 lines), agents active at crash, restart attempt
number. Inspect via `oc_serve_crashes` tool, `hermes oco serve-crashes`, or the dashboard
endpoint.

## Requirements

| | Requirement | Notes |
|---|---|---|
| 🐍 | [hermes-agent](https://github.com/NousResearch/hermes-agent) | Any modern version. Plugin loads in-process via the hermes plugin loader. |
| 🤖 | [opencode](https://opencode.ai) on `PATH` | `curl -fsSL https://opencode.ai/install \| bash` |
| 🔧 | [`gh` CLI](https://cli.github.com/) authenticated | `gh auth login` — used by the executor to open PRs and by the orchestrator to poll merge state. |
| 🌿 | `git >= 2.40` | Worktree commands. |
| 🦴 | `httpx`, `httpx-sse`, `PyYAML` in hermes' Python venv | Shipped with hermes-agent by default. `pip install -r requirements.txt` if missing. |

For dashboard, run hermes via `hermes dashboard` (no extra auth beyond the hermes session token).
For executor / reviewer LLM calls, opencode brings its own auth (`opencode auth login`). The
awaiting-input classifier uses your global hermes auxiliary-tasks provider — no separate API
key.

## Install

```bash
# canonical — clones into ~/.hermes/plugins/hermes-opencode/
hermes plugins install that-ambuj/hermes-opencode

# local dev — symlinks this checkout into ~/.hermes/plugins/hermes-opencode/
./install.sh

# then enable
hermes plugins enable hermes-opencode
```

## Configuration

All knobs live under `plugins.entries.hermes-opencode` in `~/.hermes/config.yaml`. Every section
is optional; defaults below match what the plugin uses out of the box.

```yaml
plugins:
  enabled:
    - hermes-opencode
  entries:
    hermes-opencode:
      opencode_server:
        host: "127.0.0.1"       # 0.0.0.0 to expose on the LAN
        port: 4096
        password: "${OPENCODE_SERVER_PASSWORD}"
        # When the executor fails to author its own PR, the plugin spawns
        # one-shot opencode sessions per fallback model until one succeeds.
        # Defaults avoid Anthropic models because Claude is the most common
        # rate-limit victim.
        pr_fallback_models:
          - "openai/gpt-5.5"
          - "opencode/deepseek-v4-flash-free"
      pr:
        base_branch: main
      auto_spawn_server: true   # spawn `opencode serve` automatically if down
      review:
        max_cycles: 1
      bootstrap:
        auto_on_first_spawn: true
      notify:
        # sinks defaults to ["gateway","dashboard"] when a <PLATFORM>_HOME_CHANNEL
        # env var is detected; otherwise ["cli","dashboard"]. Explicit value wins.
        sinks: ["gateway", "dashboard"]
        gateway:
          platform: bluebubbles
          chat_id: "+15551234567"
      heartbeat:
        enabled: true
        unconditional_hours: [9, 23]
        timezone: "Asia/Calcutta"
      classifier:
        enabled: true
        task: hermes_opencode.awaiting_input
      progress_narration:
        enabled: false          # opt-in: periodic "still working" DMs
        interval_sec: 300
```

For the full event list (notifications you can mute by trimming `notify.events.enabled`), see
[CHANGELOG.md](./CHANGELOG.md). Serve-side knobs `serve_crashes_file` and
`serve_log_retention_count` live at the top of `Config` and are rarely overridden.

### Picking your classifier model

The awaiting-input LLM layer routes through hermes' auxiliary-task infrastructure. Pick your
model in your global hermes config:

```yaml
auxiliary:
  hermes_opencode.awaiting_input:
    provider: anthropic
    model: claude-haiku-4-5
```

Works with Anthropic, OpenAI, Gemini, OpenRouter, or any provider hermes routes. Falls back to
the regex result on import / network / parse / timeout errors. Set `classifier.enabled: false`
to run regex-only.

## Tools (24)

Exposed to the LLM under the `hermes_opencode` toolset. Descriptions carry "WHEN TO USE" leads
so the chat LLM picks the right tool first time.

| Tool | Purpose |
|---|---|
| `oc_project_add` / `oc_project_list` / `oc_project_show` / `oc_project_remove` / `oc_project_set_repo_path` | Project registry CRUD |
| `oc_project_regenerate_bootstrap` / `oc_project_regenerate_cleanup` | Re-run repo introspection that produces the per-project bootstrap + cleanup skills |
| `oc_spawn` | Create worktree + opencode session, forward initial prompt verbatim |
| `oc_resume_pr` | Resume work on an existing open PR (check out branch in a fresh worktree, dispatch follow-up) |
| `oc_send` / `oc_wait` / `oc_kill` / `oc_cancel` | Live-agent control surface |
| `oc_status` | Phase + session status + latest assistant snippet + classifier verdict |
| `oc_retry` | Resume FAILED -> `phase_before_failed`, NEEDS_INTERVENTION -> `phase_before_intervention`, otherwise reset retry counters |
| `oc_answer` | Forward verbatim user reply to a pending opencode `/question` or `/permission` |
| `oc_output` | Return latest assistant text from the live SSE buffer (with `/message` pull fallback) |
| `oc_review_now` / `oc_review_again` / `oc_skip_review` | Force reviewer state transitions |
| `oc_pr_status` | Poll GitHub for current PR state |
| `oc_set_notify_target` | Update gateway DM target at runtime |
| `oc_heartbeat_send_now` | Force the hourly heartbeat to fire now |
| `oc_serve_crashes` | Recent `opencode serve` crash records — exit code, signal, log tail, agents active at crash |

## Slash commands

Available in any hermes CLI or gateway chat (no LLM round-trip; the `pre_gateway_dispatch` hook
runs first).

| Command | Purpose |
|---|---|
| `/oc` | Print help |
| `/oc list [--archived]` | One-line-per-agent table |
| `/oc attach <agent_id> [--lines N]` | Last N lines of the agent's transcript |
| `/oc questions` | Every pending opencode question with options inline |
| `/oc cancel <agent_id> [reason ...]` | Wind down an agent without merging |
| `/oc retry <agent_id>` | Resume FAILED / NEEDS_INTERVENTION / stuck agent |
| `/oc doctor` | Plugin health report (paste into bug reports) |
| `/oc test-notify [message ...]` | Force a full notify fanout with per-sink ok/FAIL |
| `@<agent_id> <text>` | Forward `<text>` VERBATIM to that agent's session, bypassing the chat LLM |

## CLI

`hermes oco …` is the same surface available outside an active hermes chat. Reads on-disk state
directly; no background event loop required.

```bash
hermes oco list [--archived]
hermes oco status <agent_id> [--json]
hermes oco attach <agent_id> --lines 200
hermes oco kill <agent_id> --force [--keep-worktree]
hermes oco cancel <agent_id> [reason ...]
hermes oco retry <agent_id>
hermes oco projects
hermes oco spawn <project> <task> <prompt ...>
hermes oco resume-pr <project> <pr_number> <prompt ...>
hermes oco serve-crashes [--limit N] [--json]
```

## State machine

```
CREATED -> QUEUED? -> BOOTSTRAPPING -> EXECUTING -> AWAITING_HUMAN? ->
IDLE_TASK_COMPLETE -> REVIEW_SPAWNING -> REVIEWING ->
REVIEW_DELIVERED -> EXECUTOR_ADDRESSING -> AWAITING_HUMAN? ->
IDLE_REVIEW_ADDRESSED -> COMMITTING -> PR_OPEN -> DONE

(every phase can transition into RATE_LIMITED or NEEDS_INTERVENTION
 and resume back to its saved prior phase)
```

Terminal: `DONE` (merged, archived after 12 h), `CANCELLED` (manual or auto on PR closed),
`KILLED` (hard-deleted), `FAILED` (kept indefinitely until recovered via `oc_retry` or killed).
Non-terminal but blocking: `AWAITING_HUMAN`, `NEEDS_INTERVENTION`, `RATE_LIMITED`, `QUEUED`.

Phase-by-phase semantics live in [AGENTS.md](./AGENTS.md).

## Agent naming

Agent ids are `<abbrev>/<task>`, max 20 chars:

- `abbrev` auto-derived from the project label (`dodo-payments` -> `dp`,
  `dodo-backend-bookings` -> `dbb`) or set via `oc_project_add(abbrev=...)`.
- `task` is caller-supplied and slugified to kebab-case.
- Collisions append `-2`, `-3`, ... trimming the task slug to fit.

## State directory

Lives under `~/.hermes/plugins/hermes-opencode/`:

| Path | Contents |
|---|---|
| `projects.json` | Registered projects |
| `agents.json` | Live + archived agents (archived rows survive 12 h post-DONE for audit) |
| `notifications.jsonl` | Dashboard event feed (append-only, rotated at 1000 lines) |
| `events.log` | Structured event log (append-only, rotated at 5000 lines) |
| `history.jsonl` | Archive of DONE / CANCELLED transitions (30-day retention) |
| `serve_crashes.jsonl` | Structured `opencode serve` crash records |
| `wt/<agent_id_fs>/` | Per-agent git worktree (`/` in agent id encoded as `__`) |
| `wt/<agent_id_fs>.review/` | Reviewer's sister worktree (auto-staged + torn down) |
| `logs/opencode-serve.*.log` | Per-spawn `opencode serve` stdout+stderr (newest 50 retained) |
| `logs/<agent_id_fs>.jsonl` | Per-agent activity log |

## Coexistence with oh-my-openagent

If you run [oh-my-openagent](https://github.com/code-yeongyu/oh-my-opencode) (OMO) as an
opencode plugin (registered globally via `~/.config/opencode/opencode.json`), it injects
directives with the prefix `[SYSTEM DIRECTIVE: OH-MY-OPENCODE - <TYPE>]`. This plugin uses the
parallel prefix `[SYSTEM DIRECTIVE: HERMES-OPENCODE - <TYPE>]` so the two coexist cleanly —
OMO's parser keys on the OH-MY-OPENCODE marker specifically.

## Development

Architecture, load-bearing invariants, and the per-phase state-machine contracts live in
**[AGENTS.md](./AGENTS.md)** — read that before editing.
Per-release notes live in **[CHANGELOG.md](./CHANGELOG.md)**.

### Tests

```bash
cd tests
python -m pytest .
```

`tests/pytest.ini` keeps the rootdir at `tests/` so pytest never tries to import the package
`__init__.py` as a test module. CI runs the same on Python 3.10, 3.11, 3.12.

### Smoke test

```bash
uv run --quiet scripts/phase0_opencode_spike.py --port 4099
```

Exercises every HTTP contract this plugin depends on against a live `opencode serve`. Keep it
green.

### Dashboard

The tab is a React component bundled to a single IIFE for the host's plugin loader. Edit the
source, NOT the build artifact:

```
dashboard/
├── manifest.json
├── plugin_api.py        # FastAPI router (read-only, mounted at /api/plugins/hermes-opencode/)
├── src/index.jsx        # CANONICAL SOURCE
└── dist/
    ├── index.js         # build output (committed; hermes loads this at runtime)
    └── style.css        # plain CSS (no build step)
```

```bash
cd dashboard
bun install
bun run build           # src/index.jsx -> dist/index.js
bun run watch           # rebuild on save
```

The host injects `window.__HERMES_PLUGIN_SDK__` (React + utils) and
`window.__HERMES_PLUGINS__.register(name, Component)` at load time. Auth uses
`X-Hermes-Session-Token` from `window.__HERMES_SESSION_TOKEN__`. Use the host's `--color-*` CSS
variables (`--color-foreground`, `--color-muted-foreground`, `--color-border`, `--color-card`,
`--color-primary`, `--color-ring`, `--font-mono`) — generic ones like `--foreground` don't
exist.

## License

[MIT](./LICENSE).
