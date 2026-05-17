# hermes-opencode — Developer Notes for AI Agents

Instructions for AI coding assistants working on this hermes-agent plugin.
Read this **before** editing. Conventions here are load-bearing — violating
them silently breaks behaviour the test suite doesn't cover.

## What this is

A hermes-agent plugin (Python, in-process) that orchestrates multiple
opencode agents in git worktrees. Plugin loads at hermes startup, registers
19 tools + 3 lifecycle hooks + 1 dashboard tab, spawns a singleton bg
asyncio loop that drives a per-agent state machine through executor →
reviewer → commit → PR_OPEN → DONE.

## Plugin runtime contract

The plugin is loaded by hermes' `PluginManager` via
`importlib.util.spec_from_file_location` with `submodule_search_locations`
set to the repo root. Concretely:

- `__init__.py` is loaded as `hermes_plugins.hermes_opencode`
- Submodules use **relative imports only** (`from .config import Config`)
- All `.py` files at the repo root form one package — do not put them in a
  subdirectory

The hermes Python interpreter runs the plugin. There is no plugin-owned
venv at runtime. Dependencies (`httpx`, `httpx-sse`, `PyYAML`) must be
present in the host hermes venv; document additions in `requirements.txt`
and `after-install.md`.

## Tool schema convention (LOAD-BEARING)

Every tool schema in `tools.py` MUST follow this exact shape:

```python
TOOL_NAME_SCHEMA = {
    "name": "oc_tool_name",
    "description": "What it does, when to use it, side effects.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": { ... },
        "required": [ ... ],
    },
}
```

**Do NOT** pass `description=` as a separate kwarg to `ctx.register_tool` —
hermes' `ToolRegistry.register` silently drops it. **Do NOT** put `type` /
`properties` / `required` at the top level of the schema; they must be
nested under `parameters`. This was the 0.3.0 → 0.3.1 bug: schemas
registered correctly but the LLM saw 18 tools with no descriptions and
ill-formed parameter shapes. Reference: `plugins/spotify/tools.py` in
hermes-agent is the canonical example.

## Sync vs async opencode endpoints (LOAD-BEARING)

The opencode HTTP API has two flavours of sending a prompt:

| Endpoint | Blocks? | Use from |
|---|---|---|
| `POST /session/:id/message` | Yes — streams full assistant turn | bg event-loop only |
| `POST /session/:id/prompt_async` | No — queues + returns | tool handlers, CLI subcommands |

Rule: **any code path called synchronously by a hermes tool dispatcher must
use `send_message_async`.** Calls inside `event_loop._phase_*` may use the
blocking `send_message` freely — they only block their own thread.

The 0.3.1 → 0.3.2 bug: `oc_spawn` called `send_message`, which blocked the
hermes main session for 30 s – several minutes while the executor's first
turn streamed. Fixed by switching to `send_message_async`; the bg loop
picks up first-turn completion via `/wait` + question polling.

v0.14.5 added `Config.serve_hostname` (YAML
`plugins.entries.hermes-opencode.opencode_server.serve_hostname` or env
`OPENCODE_SERVE_HOSTNAME`) so a user can pin the `--hostname=` value
`opencode serve` binds to independently of `server_url`. Default `None`
falls back to the host parsed out of `server_url` (existing v0.3.0+
behaviour). Typical use: bind to `0.0.0.0` so other machines on the LAN
can reach the server while hermes itself keeps connecting via the
loopback `server_url`. `OpencodeClient.__init__` stores the connect host
in `self._host` (used for the readiness probe + httpx target) and the
bind host in `self._serve_hostname` (used for the `--hostname=` spawn
flag); the two diverge only when this knob is set.

The 0.14.3 → 0.14.4 regression: the same anti-pattern survived in `oc_send`
all the way until v0.14.3 — `make_send` was awaiting the blocking
`send_message` with `timeout_sec=600`, so any chat-side `oc_send` call
froze the hermes main session for the duration of opencode's reply (users
perceived this as "the message is queued"). v0.14.4 migrated `oc_send` to
`send_message_async`, dropped the `timeout_sec` schema param (the queue
POST has its own 30s ceiling), and the tool result no longer carries the
agent's reply text — the bg event loop polls for the assistant reply via
SSE buffer + `get_messages` exactly as it does for `oc_spawn`, and the
existing awaiting-input / pending-question notifications surface progress
to the user.

## State machine

Each agent transitions through phases driven by `event_loop._phase_*`
handlers. Phases (see `state.py::PHASES`):

```
CREATED → BOOTSTRAPPING → EXECUTING → IDLE_TASK_COMPLETE →
REVIEW_SPAWNING → REVIEWING → REVIEW_DELIVERED → EXECUTOR_ADDRESSING →
IDLE_REVIEW_ADDRESSED → COMMITTING → PR_OPEN → DONE
```

Terminal phases: `DONE`, `FAILED`, `KILLED`. The pruner removes `DONE`
agents 4 h after merge.

Critical idle-detection rule in `_phase_executing`:
- `session.status.type == "idle"`
- no pending `/question` entries for this session
- no pending `/permission` entries for this session
- worktree has uncommitted or unpushed changes
- 30 s of stable idleness (debounce)

All four must hold to transition out of `EXECUTING`. The debounce exists
because opencode may briefly report idle between tool calls.

## Executor-driven PR open (LOAD-BEARING)

After reviewer LGTM (or `decide_review_action(...)` returns `exhausted`),
`event_loop._phase_committing` does NOT run `gh pr create --fill` itself
anymore. Instead it sends a structured prompt to the executor's existing
opencode session (`reviewer.executor_open_pr_prompt(...)`) telling the
executor to:

1. Commit any pending diff under the user's normal git identity (NO
   `-c user.email=...` / `-c user.name=...` overrides).
2. Push the branch.
3. Run `gh pr create --base <base> --title <concise> --body <markdown
   summary>` with a title and body the executor authors itself.
4. Emit `PR_OPENED: <github pr url>` on its own line.

The plugin parses the line via `reviewer.parse_pr_opened(...)` and sets
`agent.pr_url` / `agent.pr_number`. Falls back to the original plugin-
driven `reviewer.finalize_and_open_pr(...)` (which calls `pr.open_pr`
with `--fill`) when extraction fails — that fallback is the safety net,
not the default path.

Why: the executor has full context for what was changed and why, so its
PR title and body are concrete and relevant. The previous default
generated `_pr_title_from_agent_id(agent_id)` (e.g. `"Fix bb gateway"`
from `oco/fix-bb-gateway`) and a body that was just the verbatim initial
prompt — useless on review.

If you add new phases or shortcuts that bypass `_phase_committing`,
either route them through this same executor-driven path or document
why the slug-based fallback is acceptable for that path.

v0.14.6 strengthened this contract after observing every PR opened in
v0.14.x was going through the slug-based fallback (executor not emitting
the sentinel line). Changes:

- `executor_open_pr_prompt(...)` now explicitly forbids `gh pr create --fill`
  (which would pull from the pre-review staging commit and produce
  garbage), shows a concrete `PR_OPENED:` URL example to copy the shape
  of, and notes the executor MAY amend the staging commit so the final
  history reflects what actually changed.
- `parse_pr_opened(text)` accepts three formats: strict
  `PR_OPENED: <url>` (primary), permissive
  `PR opened / Opened PR / PR url / PR_OPENED ... <url>` (variant), and
  bare github.com/.../pull/N URL (last-resort fallback).
- `executor_open_pr(...)` logs the executor's full response (truncated
  to 4KB) at WARNING when parsing fails, so future drift is debuggable
  from the orchestrator log without re-running the failure.

## Awaiting-input gate and classifier cascade (LOAD-BEARING)

`_phase_executing` and `_phase_executor_addressing` do NOT transition the
agent forward (to `IDLE_TASK_COMPLETE` / `COMMITTING`) the moment the
worktree has a non-empty diff and opencode reports the session as idle.
They first ask the awaiting-input cascade whether the executor's last
assistant message is plausibly waiting on a human reply.

Cascade in [awaiting_input.py](awaiting_input.py):

1. **Regex layer** — 10 patterns (trailing `?`, "which option", "should I",
   "would you prefer", "let me know", "please confirm", "y/n", "awaiting
   your input", labeled options). Always runs. Cost zero.
2. **LLM layer** — invokes hermes' canonical auxiliary client
   `agent.auxiliary_client.async_call_llm(task=cfg.classifier_task_name, ...)`.
   The task name (default `hermes_opencode.awaiting_input`) routes
   through hermes' `auxiliary.<task>.{provider,model}` config, so the
   user picks their model in `~/.hermes/config.yaml` and we work with
   Anthropic, OpenAI, Gemini, OpenRouter, or any other provider hermes
   routes. Falls back to the regex result on `ImportError`, network
   error, parse error, or timeout.
3. **Stalled-idle reminder** — `_awaiting_input_reminder_loop()`
   re-notifies `awaiting_human` for any `EXECUTING` / `EXECUTOR_ADDRESSING`
   agent whose `last_awaiting_notify_at` is older than
   `cfg.awaiting_input_reminder_interval_sec` (default 30min).

Why this gate exists: opencode's `Message` schema has NO field that
distinguishes "asked a question" from "made a statement" — the assistant's
last text part looks identical in both cases. Without the gate, an
executor that emits "I see two options. Which would you prefer?" with a
non-empty diff would be sent straight to the reviewer, which would LGTM
or reject incomplete work and trigger a premature PR open. The cascade
catches the cases where the executor noncompliantly used plain text
instead of the `/question` API.

The `/question` API is still the authoritative signal. The
`ORCHESTRATOR_DIRECTIVE` in `tools.py` instructs the executor to use it.
The cascade is the safety net for noncompliance, not the primary path.

When extending the state machine: any phase that wants to gate review
or commit on "executor is plausibly done" must call
`_awaiting_input_blocks_review(agent)` before transitioning. Do NOT
re-implement the cascade inline.

## Error surfacing (LOAD-BEARING)

The orchestrator has TWO orthogonal error-surfacing paths. Both are
load-bearing; silently swallowing either side leaves agents stalled in
a broken state with no user-visible signal.

### Tick-failure side (orchestrator / transport exceptions)

`_agent_loop` catches every exception from `_tick`, calls
`_record_tick_failure(agent, exc)` which writes `last_tick_error` /
`last_tick_error_at` / `consecutive_tick_failures` on the agent record.
v0.14.6 added two thresholds on this path:

1. **First failure of a streak** fires a `tick_error` notification via
   `_notify_event(...)`. The `tick_error` kind is in the default
   `notify_events` set since v0.14.6. Subsequent consecutive failures
   do NOT re-notify (avoids spam from transient network errors). A
   successful tick clears `consecutive_tick_failures` to 0 via
   `_clear_tick_failure`, resetting the "first of a streak" detection.

2. **`TICK_FAILURE_ESCALATION_THRESHOLD = 3` consecutive failures**
   escalates the agent to `phase=FAILED` with
   `last_error = "stalled after N consecutive tick failures: <summary>"`
   and fires the existing `failed` notification via
   `_maybe_notify_phase`. Tasks are cancelled via `_cancel_agent_tasks`.
   Terminal agents (DONE/KILLED/FAILED/CANCELLED) are NOT re-escalated.

### Message-level side (opencode aborts inside a "successful" turn)

Opencode marks an aborted assistant turn by setting
`message.error = { name, message }` (e.g. `MessageAbortedError`,
`"Interrupted"`) on the assistant message. The error is a structured
field on the message itself, NOT a text part. The existing
`_last_assistant_text` / `_fetch_last_assistant_text` readers in
event_loop.py only walk `parts[].text`, so they miss aborts entirely.

v0.14.6 added `_message_error(item)` to extract the structured error
and `_check_executor_abort(agent)` which runs from both
`_phase_executing` and `_phase_executor_addressing` immediately after
`wait_idle` succeeds. The handler is idempotent on `message.id`: each
new abort fires exactly one `aborted` notification and queues exactly
one `continue` follow-up via `send_message_async`. Same-id re-observations
are noops. Forward progress (no error on the latest message) clears
`consecutive_aborts` and `last_abort_msg_id`.

After `ABORT_ESCALATION_THRESHOLD = 3` distinct aborts (different
`message.id`) the agent is escalated to `phase=FAILED` with
`last_error = "executor aborted N consecutive times: <name>: <msg>"`,
firing the existing `failed` notification.

The `aborted` event kind is in the default `notify_events` set since
v0.14.6. Body template lives in `_default_event_body`.

When adding new phases or new tool-error paths: fire `tick_error` /
`aborted` events through the same helpers. NEVER add a silent
`except Exception: pass` branch on these paths.

## OMO directive coexistence (LOAD-BEARING)

Every executor session runs inside an opencode instance that loads
[oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent) (OMO)
as an opencode plugin (registered globally via
`~/.config/opencode/opencode.json::plugin`). OMO injects directives into
the executor's conversation using the prefix
`[SYSTEM DIRECTIVE: OH-MY-OPENCODE - <TYPE>]` and `<system-reminder>`
tags.

hermes-opencode uses the parallel prefix
`[SYSTEM DIRECTIVE: HERMES-OPENCODE - ORCHESTRATOR RULES]` ... `[END
SYSTEM DIRECTIVE]` for the directive wrapped around the initial prompt
in `tools.py::make_spawn`. OMO's `isSystemDirective()` parser checks for
the OH-MY-OPENCODE prefix specifically, so our HERMES-OPENCODE directive
flows through OMO untouched.

When adding new orchestrator-emitted directives:

- Use the `[SYSTEM DIRECTIVE: HERMES-OPENCODE - <TYPE>]` ... `[END SYSTEM
  DIRECTIVE]` envelope. Keep the user's verbatim prompt unchanged below
  it.
- Never use the OH-MY-OPENCODE prefix.
- Pick a `<TYPE>` that is unique within hermes-opencode (so future
  grep / parser work can target specific directives).

## CANCELLED vs KILLED (LOAD-BEARING)

Two terminal phases mean different things:

| Phase | Record in `agents.json` | When to use |
|---|---|---|
| `CANCELLED` | **kept** (carries `cancellation_reason`, `cancelled_at`); archived after 12 h like DONE | task abandoned without merging — manual via `oc_cancel` / `/oc cancel` / `hermes oco cancel`, OR automatic via `_phase_pr_open` when GitHub reports `state == "CLOSED"` |
| `KILLED` | **removed** | broken / wrong agent you want erased entirely |

`oc_cancel` runs the full cleanup sequence (delete sessions, teardown
reviewer worktree, run cleanup skill, remove executor worktree). It
refuses on already-terminal agents (`DONE`, `KILLED`, `CANCELLED`).

Auto-cancel on PR closed runs through the same shared
`event_loop._cleanup_worktrees(agent, worktree)` helper that the
MERGED → DONE branch uses. If you add new terminal transitions, route
them through that helper so they get sister teardown + cleanup skill +
worktree removal in the same order.

`cancelled` is in the default `notify.events.enabled` set; the
`_default_event_body` renders `Agent cancelled. Reason: ...`. Don't add
a new terminal phase without a notify event — silent terminal
transitions are confusing.

## Auto-detected DM channel (LOAD-BEARING)

`Config.from_plugin_entry` scans `os.environ` for
`<PLATFORM>_HOME_CHANNEL` in this priority order
(`config.HOME_CHANNEL_PLATFORMS`):

`bluebubbles, telegram, discord, slack, teams, google_chat, feishu,
wecom, line, irc, mattermost, sms, qqbot`

The first match populates `notify_gateway_platform` +
`notify_gateway_chat_id`. When both are set (whether via the env
auto-detect path OR explicit plugin entry), the default `notify.sinks`
becomes `["gateway", "dashboard"]` instead of `["cli", "dashboard"]` —
so a user with a home channel configured gets DM notifications without
touching `plugins.entries.hermes-opencode`.

Override precedence:
1. Explicit `plugins.entries.hermes-opencode.notify.sinks` always wins.
2. Explicit `notify.gateway.platform` + `notify.gateway.chat_id`
   overrides env auto-detect.
3. When `platform` is set but `chat_id` isn't, we still read
   `<PLATFORM>_HOME_CHANNEL` for the chat id — preserves the v0.11.0
   behaviour.

`Config.notify_discovery_source` records where the gateway target came
from (`explicit`, `env:<VAR>`, or `None`). `/oc doctor` surfaces it.

If you add a new platform to `HOME_CHANNEL_PLATFORMS`, put it where
its priority sits (more common → earlier). Don't reorder existing
entries without a CHANGELOG note — users may have multiple home
channels set and rely on the priority order.

## Archived agents (LOAD-BEARING)

`Agent` carries `archived: bool` and `archived_at: float | None`. The
pruner (`event_loop._pruner_loop`, 60 s tick) sets `archived=True` on
DONE agents older than `ARCHIVE_AFTER_SEC = 12 h`. The row STAYS in
`agents.json` — there is no longer a hard-delete after 4 h. Archive
records continue to be appended to `history.jsonl` for audit via
`_archive_done(...)` exactly as before; the difference is the live row
survives.

Default surfaces hide archived agents:
- `/oc list` filters `archived=True` out; `/oc list --all` re-includes.
- `hermes oco list` filters; `--all` / `-a` re-includes.
- Dashboard `/agents` endpoint hides archived; pass
  `?include_archived=1` to re-include. The events WebSocket honours the
  same query param at connect time. The frontend exposes a `show
  archived` checkbox.

When adding NEW listing surfaces, default-hide archived and document
the include knob. Never re-add a hard-delete path — that would lose the
PR-merge audit trail the archived rows preserve.

## Reviewer worktree isolation (LOAD-BEARING)

Two opencode sessions targeting the same `x-opencode-directory` SHARE
state (opencode's `InstanceStore` is dir-keyed). Concurrent writes are
NOT protected.

So the reviewer ALWAYS runs in a sister worktree at
`<executor_worktree>.review/`, created via `git worktree add --detach`
from the executor's branch. The reviewer session uses `agent="plan"` —
opencode's read-only built-in agent — which can't accidentally edit
files. After the review classifier emits LGTM or REQUESTS_CHANGES, the
sister worktree is torn down via `git worktree remove --force`.

## Atomic state writes

`projects.json`, `agents.json`, `notifications.jsonl`, `history.jsonl`
all live under `~/.hermes/plugins/hermes-opencode/`. JSON file
writes go through this pattern (see `projects.py::_write`):

```python
with tempfile.NamedTemporaryFile(
    "w", dir=path.parent, prefix=path.name + ".",
    suffix=".tmp", delete=False, encoding="utf-8",
) as f:
    json.dump(data, f, indent=2, sort_keys=True)
    tmp = Path(f.name)
tmp.replace(path)
```

Never write directly with `path.write_text(...)`. Locks are
`threading.Lock` instances per registry/store instance — guards in-process
contention only, not multi-process.

## Dashboard API surface

`dashboard/plugin_api.py` exposes a read-only FastAPI router mounted at
`/api/plugins/hermes-opencode/`. Every endpoint is informational; mutation
goes through the tool surface. Endpoints:

| Path | Returns |
|---|---|
| `GET /` | Plugin info + endpoint enumeration |
| `GET /health` | State directory + file existence checks |
| `GET /config` | `{plugin, server_url}` — the configured `opencode serve` URL surfaced for the dashboard header |
| `GET /agents?include_archived=0` | `{agents[], count, archived_hidden, server_url}` — each row carries `session_url` (and `reviewer_session_url` when applicable), constructed as `<server_url>/session/<session_id>/message` |
| `GET /agents/{agent_id}` | `{agent}` with the same `session_url` injection |
| `GET /projects` | `{projects[], count}` |
| `GET /projects/{label}` | `{project}` |
| `GET /heartbeats?n=20` | Recent `notifications.jsonl` entries |
| `GET /history?n=50` | Archived agents from `history.jsonl` |
| `WS /events?token=...&include_archived=0` | Streaming snapshot + agents/heartbeat deltas. The initial `"snapshot"` payload and subsequent `"agents"` pushes carry `server_url` so the frontend can render the dashboard header even when never hitting the REST endpoints. |

The session URL is a real opencode HTTP endpoint (returns the session's
message list as JSON) — opening it in a browser without the
`x-opencode-directory` header will fail, but the URL is the canonical
copyable handle for `curl` / API inspection. There is no opencode HTML
session UI to link to; the agent-detail modal in the dashboard is the
human-facing inspection surface and is opened by clicking an agent row.

`_server_url_from_config()` lazily imports `..config` inside the dashboard
package. If the import fails (e.g. plugin_api is loaded out of context),
the helper returns `""` and downstream `_make_session_url` returns `None`
so the frontend skips rendering broken links.

## Dashboard development

The dashboard tab is a React component bundled to one IIFE for the host's
plugin loader. The host injects `window.__HERMES_PLUGIN_SDK__` (React +
utils) and `window.__HERMES_PLUGINS__.register(name, Component)` at load
time. Auth uses `X-Hermes-Session-Token` from
`window.__HERMES_SESSION_TOKEN__`.

```
dashboard/
├── manifest.json            # tab path, icon, entry, api, css
├── plugin_api.py            # FastAPI router (read-only routes)
├── package.json             # devDep: esbuild
├── src/
│   └── index.jsx            # CANONICAL SOURCE — edit here
└── dist/
    ├── index.js             # build output (committed; hermes loads this)
    └── style.css            # plain CSS (no build step)
```

**Edit `src/index.jsx`, then rebuild:**

```bash
cd dashboard
bun install     # one-time
bun run build   # src/index.jsx → dist/index.js
bun run watch   # rebuild on save
```

`dist/index.js` is committed because hermes `plugins install` clones the
repo without running any build step. CI should verify the committed
artifact matches what `bun run build` produces from the current source —
add this when the project takes on contributors.

**CSS variables** the host actually defines (use these, NOT generic ones
like `--foreground` / `--muted`):
`--color-foreground`, `--color-muted-foreground`, `--color-border`,
`--color-card`, `--color-primary`, `--color-ring`, `--font-mono`.

The 0.3.2 → 0.3.3 bug: I used `var(--foreground, #ddd)` etc., variables
that don't exist. Fallbacks rendered into the host's theme and most text
was invisible (only highlighted on selection).

## Project key derivation

`projects.py::project_key_for(repo_path)` uses
`sha256(remote.origin.url)[:12]` so the same repo cloned to different
paths maps to the same key. Falls back to `sha256(resolved_path)[:12]`
prefixed `proj_local_` when no remote is configured. Never compute the
key from `repo_path` alone — it would silently fork project history when
the user moves their checkout.

## Agent naming

`worktree.py::compose_agent_id(abbrev, task, existing)` produces
`<abbrev>/<task>` capped at 20 chars (`AGENT_ID_MAX = 20`). On collision
with `existing`, appends `-2`, `-3`, … with the task slug trimmed to
fit. Filesystem encoding uses `__` for `/` (so `dp/refunds` →
`wt/dp__refunds/`).

## Background event loop

`event_loop.py` runs a single asyncio loop in a daemon thread. Started
from `Runtime.on_session_start` and (idempotently) from `oc_spawn`.
Components:

- `_supervisor()` — every 2 s, ensures every non-terminal agent has a
  running `_agent_loop` task
- `_agent_loop(agent_id)` — invokes the correct `_phase_<phase>` handler;
  exponential backoff on tick failures
- `_pruner_loop()` — every 60 s, archives DONE agents older than 4 h to
  `history.jsonl` and drops them from `agents.json`
- `_heartbeat_loop()` — sleeps until next top-of-hour, sends heartbeat
  via `notify.fanout` if inside the day window or there are pending tasks

All four tasks are cancelled on `event_loop.stop()`.

## Testing

```bash
cd tests
python -m pytest .
```

`tests/pytest.ini` keeps pytest's rootdir at `tests/` so it never tries
to import the package's `__init__.py` as a test module (which would crash
on relative imports). Do not move pytest.ini to the repo root.

Tests cover pure-logic helpers only — slug derivation, registry CRUD,
heartbeat formatting, reviewer classification, bash extraction. Tests
that need a live opencode (Phase 0 / 1 / 5 smoke scripts) live under
`scripts/` and are run manually.

## Gateway slash-command dispatch (LOAD-BEARING)

Calling `ctx.register_command("oc", handler=..., ...)` alone makes the
slash command work in the CLI but **NOT** in the gateway (iMessage,
Telegram, Discord, Slack). The gateway uses a different code path that
only resolves built-in commands through `hermes_cli.commands.COMMAND_REGISTRY`;
plugin commands aren't visible to it.

The pattern (lifted from `eng-task-system`): also register a
`pre_gateway_dispatch` hook that intercepts `/oc …` messages BEFORE the
gateway's built-in dispatcher rejects them, dispatches inline via the
same `make_oc_dispatcher` handler used by the CLI path, echoes the
result back via the channel adapter, and returns
`{"action": "skip", "reason": "..."}` to short-circuit the rest of the
gateway flow.

Helpers are in `__init__.py`:
- `_pre_gateway_dispatch_hook(event=None, gateway=None, **_)` — the hook
- `_gateway_send(gateway, event, message)` — echo helper, tries the
  in-process `_gateway_runner_ref().adapters` first, falls back to
  `hermes send-message --target <platform>:<chat_id> <message>`
  subprocess when the runner isn't reachable in the current process

The hook returns `None` (passes through) when:
- `event` is None
- `_runtime` or `_oc_dispatcher_cache` isn't initialized yet
- the message doesn't start with `/oc` followed by EOS or whitespace
  (so `/oc-list`, `/oclist`, and unrelated chat pass straight through)

The 0.9.0 → 0.9.1 bug: registered `/oc` via `register_command` only;
worked in CLI, silently failed in iMessage with "Unknown command".

When adding new slash commands in the future, register BOTH ways or
update `_pre_gateway_dispatch_hook` to match the new prefixes.

## Gateway `@<agent_id>` direct dispatch (LOAD-BEARING)

Sibling to the `/oc` slash-command path. `_handle_at_agent_dispatch` runs
inside `_pre_gateway_dispatch_hook` BEFORE the `/oc` parser. Pattern:

```
@<agent_id> <body>
```

where `agent_id` matches `[A-Za-z0-9][A-Za-z0-9_-]*/[A-Za-z0-9][A-Za-z0-9_-]*`
(the `worktree.compose_agent_id` charset). When the agent_id resolves to a
live, non-terminal agent, the body is forwarded VERBATIM to the agent's
opencode session via `send_message_async` (fire-and-forget) and the gateway
short-circuits with `{"action": "skip"}`. The hermes chat LLM never sees
the message: zero paraphrasing surface, zero blocking on opencode's reply.

Resolution semantics (do not change without a CHANGELOG note):

- Message doesn't start with `@` → fall through (None).
- `@` prefix present but the agent_id doesn't match the regex → fall
  through (None). Example: `@username hi` from a group chat has no `/`
  so it never matches; the chat LLM still sees it.
- Regex matches but the agent_id does not resolve in `rt.agents` → fall
  through (None) silently. Critical for not eating unrelated `@mentions`
  in group chats just because their format happens to match.
- Agent is in a terminal phase (`DONE` / `KILLED` / `FAILED` /
  `CANCELLED`) → echo `[hermes-opencode] cannot dispatch to @<id>:
  phase=<phase>` and skip.
- Body is empty (`@<id>` alone) → echo
  `[hermes-opencode] empty message; use @<id> <text>` and skip.
- Valid dispatch → echo `[hermes-opencode] -> @<id>` (or
  `... failed: <exc>` on transport error) and skip.

Async bridging from this sync hook uses the same dual pattern as
`_gateway_send`: try `asyncio.get_running_loop()` and schedule via
`loop.call_soon_threadsafe(asyncio.ensure_future, ...)` when the gateway
is in an async context; fall back to `asyncio.run` when called from a
sync test or non-async caller.

Tests in `tests/test_pure_logic.py::TestAtAgentDirectDispatch` pin every
resolution branch. Add cases there when extending the regex or adding new
short-circuit reasons.

## Anti-patterns (BLOCKING — reject in review)

- **Tool descriptions passed via `register_tool(description=...)` kwarg.**
  Must live inside the schema dict. See spotify plugin for the convention.
- **`send_message` (sync) called from a hermes tool handler.** Use
  `send_message_async`. Sync blocks hermes' main session.
- **Hand-edited `dashboard/dist/index.js`.** Edit `dashboard/src/index.jsx`
  and rebuild. `dist/` is build output even though we commit it.
- **CSS variables that don't exist** (`--foreground`, `--muted`,
  `--border`). Use the host's `--color-*` variables.
- **`path.write_text(...)` for state files.** Use the tempfile+rename
  atomic-write pattern.
- **Reviewer session sharing the executor's worktree directory.** Always
  stage a sister `<wt>.review/` worktree first.
- **Plugin-driven `gh pr create` as the default commit path.** Use the
  executor-driven path (`reviewer.executor_open_pr`). The slug-based
  `_pr_title_from_agent_id` + verbatim-prompt body is the fallback when
  the executor fails to emit `PR_OPENED:` — not the primary surface.
- **Hard-forcing git identity on plugin-side commits.** The
  `-c user.email=hermes-opencode@local -c user.name=hermes-opencode`
  overrides on the pre-review staging commit were dropped in 0.10.0.
  Don't reintroduce them; let the user's git config stand and surface a
  typed error if it's unset.
- **Hard-deleting DONE agents from `agents.json`.** Use the archive
  flag. Hard-deleting would lose the merged-PR audit trail.
- **Marketing comments, AI-slop narration, em-dashes in code.** No.
- **New top-level helpers files like `utils.py` / `helpers.py`.** Module
  names must describe what they own.

## Files at the repo root and what they own

| File | Owns |
|---|---|
| `__init__.py` | `register(ctx)` — entry point; wires tools + hooks + event loop + notify inject_message binding |
| `config.py` | `Config` dataclass; reads plugin entry from `~/.hermes/config.yaml`; paths under `~/.hermes/plugins/hermes-opencode/` |
| `transport.py` | `OpencodeClient` — async httpx wrapper around opencode HTTP API; both `send_message` (sync) and `send_message_async` (queue) |
| `worktree.py` | `git worktree` ops, `project_key_for`, `derive_abbrev`, `compose_agent_id`, `slugify` |
| `projects.py` | `ProjectRegistry` over `projects.json` |
| `state.py` | `AgentStore` over `agents.json`; `Agent` dataclass; `PHASES` set |
| `tools.py` | 19 tool schemas + handlers + `all_tool_specs(rt)`; `Runtime` dataclass |
| `event_loop.py` | Singleton bg asyncio loop + per-agent state machine + pruner + heartbeat scheduler |
| `bootstrap.py` | Shell-bash extraction from skill SKILL.md; opencode-driven recovery on failure; `generate_bootstrap_skill` |
| `reviewer.py` | Sister-worktree staging, `REVIEW: LGTM/REQUESTS_CHANGES` classifier, `finalize_and_open_pr` |
| `pr.py` | `gh pr create --fill` + `gh pr view --json` wrappers; `PrInfo` dataclass |
| `notify.py` | Sink fanout (CLI `inject_message`, gateway DM via `platform_registry.create_adapter`, dashboard JSONL append) |
| `heartbeat.py` | Hourly report builder; phase glyphs; TZ-aware day window; `_format_age`; `next_top_of_hour` |
| `dashboard/manifest.json` | Plugin manifest read by hermes' dashboard discovery |
| `dashboard/plugin_api.py` | FastAPI router (READ-ONLY; mounted at `/api/plugins/hermes-opencode/`) |
| `dashboard/src/index.jsx` | React frontend source |
| `dashboard/dist/index.js` | Build output of `bun run build` — committed but DO NOT edit by hand |
| `dashboard/dist/style.css` | Plain CSS (no build step) |

## Version + release flow

- Bump `plugin.yaml::version` and add a CHANGELOG section in the same
  commit as the user-facing change.
- Patch bumps for behaviour-preserving fixes (0.3.0 → 0.3.1 description
  fix); minor bumps for new features (0.3.x → 0.4.0 for review-cycles +
  auto-bootstrap + SSE + WebSocket).
- After commit, `git push origin main` triggers CI (`.github/workflows/ci.yml`)
  which runs `pytest` on Python 3.10 / 3.11 / 3.12.
