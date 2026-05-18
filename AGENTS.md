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
CREATED → QUEUED?→ BOOTSTRAPPING → EXECUTING ⇄ RATE_LIMITED →
IDLE_TASK_COMPLETE → REVIEW_SPAWNING → REVIEWING → REVIEW_DELIVERED →
EXECUTOR_ADDRESSING → IDLE_REVIEW_ADDRESSED → COMMITTING ⇄ RATE_LIMITED →
PR_OPEN → DONE
```

Terminal phases: `DONE`, `FAILED`, `KILLED`, `CANCELLED`. The pruner
archives terminal agents after `ARCHIVE_AFTER_SEC = 12h`.

`AWAITING_HUMAN` (v0.16.0) is the phase entered whenever the
awaiting-input cascade fires (pending `/question` or `/permission`
on the executor session, OR the classifier flags the latest assistant
text as a prose question). `_phase_executing`,
`_phase_executor_addressing`, and `_awaiting_input_blocks_review` all
route through `_enter_awaiting_human(agent, body)` which saves
`phase_before_awaiting` and `awaiting_human_since`, then fires the
`awaiting_human` event. The dedicated `_phase_awaiting_human` handler
polls each tick: if Q/P are still pending OR the classifier says the
latest text is still awaiting, sleeps briefly and returns; if both
detectors say not-awaiting, restores `phase_before_awaiting` (default
EXECUTING) and fires `awaiting_human_resumed`. The reminder loop
(`_run_awaiting_input_reminders`) scans `phase == "AWAITING_HUMAN"`
only — the prior v0.14.x scan of EXECUTING / EXECUTOR_ADDRESSING is
retired because every awaiting agent is now in AWAITING_HUMAN. `_enter_awaiting_human`
is idempotent on re-entry (preserves the existing `phase_before_awaiting`
and `awaiting_human_since` so reminder fires don't reset the clock).

`QUEUED` (v0.15.0) is the soft-queue phase entered at `oc_spawn` time
when at least one non-terminal agent is in `RATE_LIMITED`. The new
agent's worktree, session, and bootstrap are created normally but the
first turn is NOT sent. `_phase_queued` polls (every `QUEUE_POLL_SEC`)
until no `RATE_LIMITED` agents remain, then sends the wrapped initial
prompt and transitions to `EXECUTING`.

`RATE_LIMITED` (v0.15.0) is entered from `_phase_executing`,
`_phase_executor_addressing`, and `_phase_committing` whenever
`_check_executor_rate_limited(agent)` detects opencode's structured
`APIError statusCode=429` (or fallback textual marker) on the
executor's latest assistant turn. The agent saves
`phase_before_rate_limit`, records `rate_limit_retry_after_at` from
the response headers / metadata (or `RATE_LIMIT_MIN_WAIT_SEC` floor),
and `_phase_rate_limited` polls until the retry-after elapses, then
restores the saved phase. Review is NOT bypassed; the agent continues
through its own normal flow when the limit clears.

Critical idle-detection rule in `_phase_executing`:
- `session.status.type == "idle"` (authoritative server-side signal,
  cached from the `session.status` SSE event in `_sse_session_status`)
- no pending `/question` entries for this session
- no pending `/permission` entries for this session
- worktree has uncommitted or unpushed changes
- EITHER the executor emitted `READY_FOR_REVIEW` on its own line (the
  decisive sentinel; see "Review-readiness sentinel" below), OR
  `IDLE_DEBOUNCE_SEC = 120 s` of wall-clock idleness has elapsed since
  `agent.idle_since` was set
- the awaiting-input cascade does not block (see "Awaiting-input
  gate" below)

All conditions must hold to transition out of `EXECUTING`. The
120-second debounce exists because opencode may briefly report idle
between tool calls, and the previous 30-second window produced
premature reviews. The debounce is a wall-clock timestamp check on
`agent.idle_since`, NOT a blocking `asyncio.sleep` — the tick
continues to run so rate-limit, abort, and question/permission
arrivals are observed promptly. The READY_FOR_REVIEW sentinel
bypasses the 120 s debounce entirely when present.

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
2. **Todo-list override** — when the caller passes
   `has_incomplete_todos=True` (v0.17.0+) the cascade short-circuits
   with `awaiting=False, source="todo-override"`. The opencode
   todowrite tool's latest committed call is the source of truth:
   any non-completed todo (or a todowrite call still in
   pending/running state) proves the executor has work in flight,
   so prose that the regex flagged is more likely status narration
   than a real question. `event_loop._has_incomplete_todos(items)`
   parses this from `state.input.todos[*].status` on the latest
   `type="tool", tool="todowrite"` part.
3. **LLM layer** — invokes hermes' canonical auxiliary client
   `agent.auxiliary_client.async_call_llm(task=cfg.classifier_task_name, ...)`.
   The task name (default `hermes_opencode.awaiting_input`) routes
   through hermes' `auxiliary.<task>.{provider,model}` config, so the
   user picks their model in `~/.hermes/config.yaml` and we work with
   Anthropic, OpenAI, Gemini, OpenRouter, or any other provider hermes
   routes. Falls back to the regex result on `ImportError`, network
   error, parse error, or timeout.
4. **Stalled-idle reminder** — `_awaiting_input_reminder_loop()`
   re-notifies `awaiting_human` for any `EXECUTING` / `EXECUTOR_ADDRESSING`
   agent whose `last_awaiting_notify_at` is older than
   `cfg.awaiting_input_reminder_interval_sec` (default 30min).

Source-of-truth for the executor's last assistant text feeding the
cascade is `_fetch_last_assistant_text(agent)`. As of v0.17.0 the
SSE buffer:

- skips parts whose parent `message.role` is not `"assistant"`
  (tracked via `message.updated` events in `_sse_message_roles`)
- skips parts whose `type` is not `"text"` (tracked via
  `message.part.updated` events in `_sse_part_types`; required
  because opencode emits reasoning deltas with `field="text"`)

The HTTP-API fallback path applies the same role filter.

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

## Review-readiness sentinel (LOAD-BEARING, v0.17.0+)

The executor is instructed (rule 2 of `ORCHESTRATOR_DIRECTIVE` in
`tools.py`) to emit `READY_FOR_REVIEW` on a line by itself when its
task is complete and the diff is ready for code review.
`reviewer.parse_ready_for_review(text)` parses the last assistant
text for the sentinel.

When detected with a non-empty worktree diff:

- `_phase_executing` transitions directly to `IDLE_TASK_COMPLETE`
  without waiting for the 120-second idle debounce.
- `_phase_executor_addressing` transitions directly to `COMMITTING`.

The awaiting-input cascade still runs as a safety check before the
transition: if the same message somehow contains both the sentinel
and a question, the cascade wins and the agent enters
`AWAITING_HUMAN`.

Without the sentinel, the orchestrator falls back to the 120-second
idle-debounce + diff + awaiting-input cascade.

`Agent.ready_for_review_at` is stamped with `time.time()` when the
sentinel triggers a transition (audit only; the row stays put).

## Session-status cache (LOAD-BEARING, v0.17.0+, refactored v0.20.2)

opencode publishes a `session.status` SSE event whose payload is
`{sessionID, status: {type: "idle" | "busy" | "retry", ...}}`. The
status reflects the canonical server-side state of the model loop:
`busy` while a turn is running or a tool is executing, `retry`
while opencode is retrying after a recoverable failure, `idle`
otherwise.

v0.20.2 unified the per-agent status into a single typed cache
(`SessionStatusCache` in `event_loop.py`) with two authoritative
writers:

- **SSE consumer** writes via `cache.update(agent_id, status, source="sse")`
  on every `session.status` event. Sub-second latency when the
  consumer is connected.
- **HTTP poller** writes via `cache.update(agent_id, {"type": "idle"}, source="poll")`
  inside `_wait_idle_through_cache(agent_id, session_id, worktree, timeout)`
  whenever `client.wait_idle` returns True. This is the backstop
  for SSE drops (e.g. after a serve flap): opencode does NOT re-emit
  `session.status: idle` for sessions that were already idle before
  the SSE consumer disconnected, so without write-through the cache
  would stay stuck at the pre-flap `busy` / `retry` indefinitely
  (this was the BCK/p-list-tests bug that motivated v0.20.2).

Reader: `_session_status_is_idle(agent)` calls `cache.get(agent_id)`
and returns:
- True when no status has been observed (permissive default; lets
  the existing idle-detection cascade run during the window between
  session creation and first event)
- True when cached `{type: "idle"}`
- False when cached `{type: "busy"}` or `{type: "retry"}`

When False, `_phase_executing` and `_phase_executor_addressing` reset
`agent.idle_since` to None and return early. The heuristic-based
120s debounce only starts ticking once SOMEONE has written
`{type: "idle"}` to the cache (SSE or wait_idle-via-poll, last
write wins).

`cache.get_full(agent_id)` returns `(status, source, updated_at)` for
diagnostic surfaces (oc_status, dashboard).

### When extending the state machine

- All `wait_idle` calls against an executor session MUST go through
  `_wait_idle_through_cache(agent_id, session_id, worktree, timeout)`
  rather than `_runtime.client.wait_idle(...)` directly. This is what
  closes the SSE-drop gap. The reviewer-session `wait_idle` (in
  `_phase_reviewing`) does NOT use the helper because the cache
  tracks executor sessions only.
- New status writers (e.g. a future periodic `GET /session/<id>`
  watchdog) must use a distinct `source=...` tag so diagnostic
  surfaces can attribute the latest write.

## Reviewer worktree staging (LOAD-BEARING, fixed in v0.20.2)

`reviewer.stage_reviewer_worktree` runs before every REVIEW_SPAWNING
to materialise the sister `<wt>.review/` worktree from the executor's
branch. v0.20.2 fixed a latent bug where `shutil.rmtree` was nested
inside `except Exception` after `wt._git(check=False)`: since
`check=False` does not raise on non-zero exit, the rmtree was dead
code. If a previous reviewer left behind gitignored content (cargo's
`target/`, `node_modules/`, `.env`, ...) when its `git worktree
remove --force` cleaned up the worktree metadata, the directory
survived as a non-empty filesystem path. The next staging attempt's
`git worktree add --detach <path>` then failed with exit 128
(non-empty target), cascading into 3 consecutive tick failures and
FAILED escalation.

Current staging contract:

```python
if sister.exists():
    wt._git(repo_path, "worktree", "remove", "--force", str(sister), check=False)
    shutil.rmtree(sister, ignore_errors=True)
wt._git(repo_path, "worktree", "add", "--detach", str(sister), executor.branch)
```

Both calls run unconditionally on every entry to `stage_reviewer_worktree`.
`git worktree remove --force` handles the registered case; the
unconditional `rmtree` handles every other leftover (untracked /
gitignored content from prior runs, half-removed worktrees, manual
artifacts).

When adding new build steps that might leave artifacts in the sister
worktree, do NOT rely on the worktree teardown to clean them up —
staging now defensively wipes them on next entry.

## Rate limits and queue (LOAD-BEARING)

When opencode's upstream provider rate-limits the executor (most common
on Anthropic Claude), opencode emits the error as a structured field on
the assistant message:

```json
{
  "name": "APIError",
  "statusCode": 429,
  "message": "...",
  "isRetryable": true,
  "responseHeaders": { "retry-after": "60", ... },
  "metadata": { "retryAfterMs": 60000, ... }
}
```

`_message_is_rate_limited(item)` (event_loop.py) extracts the
retry-after window. `_check_executor_rate_limited(agent)` calls it
against the latest assistant message and, on a hit, transitions the
agent to `phase=RATE_LIMITED` with `phase_before_rate_limit` saved.

`_phase_rate_limited` (the wait-and-resume handler) polls until
`rate_limit_retry_after_at` elapses, then restores the saved phase and
fires `rate_limit_cleared`. The agent continues through its own normal
flow — review, executor-addressing, committing, all preserved. This
choice is intentional: review is NOT bypassed.

Concurrent spawn gating: while any agent is in `RATE_LIMITED`,
`oc_spawn` parks new agents at `phase=QUEUED` with
`queued_blocked_by=[<rate-limited agent_ids>]`. The new agent's
worktree, session, and bootstrap run normally — only the initial
prompt is deferred. `_phase_queued` polls until the registry has zero
`RATE_LIMITED` agents, then sends the wrapped initial prompt and
transitions to `EXECUTING`, firing `queue_drained`.

Notification events added in v0.15.0 (all default-on):
- `rate_limited` — agent entered RATE_LIMITED with retry-after window.
- `rate_limit_cleared` — agent restored to its prior phase.
- `queued` — new spawn parked (default body via _default_event_body).
- `queue_drained` — queued agent promoted to EXECUTING.

PR fallback when executor doesn't emit `PR_OPENED:` (separate from
rate-limit, but using the same configurable model list):
`reviewer.oneshot_open_pr(client, agent, base_branch, model_specs)`
iterates `Config.pr_fallback_models` (configurable; default
`["openai/gpt-5.5", "opencode/deepseek-v4-flash-free"]`). Each iteration
creates a FRESH opencode session in the executor's worktree with an
explicit model struct `{id, providerID, variant?}` (parsed by
`reviewer.parse_model_id` from the `provider/model[/variant]` spec
string and passed to `OpencodeClient.create_session(..., model=...)`).
First successful `PR_OPENED:` sentinel wins. All models exhausted →
agent escalates to `FAILED`. The old slug+initial_prompt fallback
(`reviewer.finalize_and_open_pr`) is no longer called from
`_phase_committing`; it survives only as a CLI-callable helper for
manual recovery.

When adding new phases: if they touch the executor session, call
`_check_executor_rate_limited` BEFORE `_check_executor_abort` at the
top of the handler. Rate-limit takes precedence over abort because
the abort detector would also fire on 429s but with less useful
context (no retry-after).

v0.15.1 generalized the detector. `_check_session_rate_limited(agent,
session_id, worktree, session_label="executor"|"reviewer")` is now the
underlying primitive; `_check_executor_rate_limited(agent)` is a
back-compat one-arg wrapper. `_phase_reviewing` calls the generic
helper with the reviewer's session and `session_label="reviewer"` so
reviewer-session 429s also transition the agent to RATE_LIMITED with
`phase_before_rate_limit=REVIEWING`. The notification body and
`agent.last_error` include the session label, so the user knows which
session was rate-limited. When adding handlers for other sessions
(reviewer-2, dashboard-driven probes, etc.) call the same generic
helper with the appropriate label.

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

## Failure-recovery architecture (LOAD-BEARING, v0.18.0+)

The state machine has three tiers of failure recovery, in order of
preference:

### 1. Phase-scoped retry budget

Every recoverable error path now calls
`_handle_phase_failure(agent, phase, summary)` instead of escalating
to `FAILED` on first error. `PHASE_RETRY_CEILING` (per-phase) and
`PHASE_RETRY_CEILING_DEFAULT = 3` govern the budget. The counter
lives on `Agent.phase_retry_count` and is auto-reset by
`AgentStore.update` whenever the agent's phase actually changes,
so the budget is naturally scoped per-phase without per-site
bookkeeping.

Converted sites:

- `_phase_queued` first-prompt send (ceiling 5)
- `_phase_review_spawning` worktree staging + session create (ceiling 5)
- `_handle_review_text` addressing-prompt dispatch (ceiling 5 via REVIEW_DELIVERED key)
- `_phase_reviewing` "reviewer state lost" re-stages instead of failing

The tick-failure escalation (`_record_tick_failure`, 3 consecutive
exceptions) and abort escalation (`_check_executor_abort`, 3 distinct
aborts) remain in place. Both stamp `phase_before_failed` so
`oc_retry` can resume.

### 2. NEEDS_INTERVENTION phase

When `_handle_phase_failure(..., on_exhausted_intervene=True)` is
called, exhausting the retry budget routes the agent to the new
non-terminal `NEEDS_INTERVENTION` phase instead of `FAILED`. The
phase carries `intervention_reason` + `intervention_since`. The
dedicated `_phase_needs_intervention` handler does nothing
autonomous: the agent waits for an operator to run `oc_retry`
(or fix whatever the reason field describes and then `oc_retry`).

The PR-fallback exhaustion in `_phase_committing` is the canonical
example: all configured `pr_fallback_models` returned no
`PR_OPENED:` sentinel, which usually means `gh auth status` is
broken or github is unreachable. That's recoverable by an operator;
no point burying the agent in `FAILED`.

`NEEDS_INTERVENTION` is in `PHASES` but NOT in `TERMINAL_PHASES`.
The supervisor still spawns its `_agent_loop`; the handler just
sleeps until external action moves it out.

### 3. `oc_retry` (`/oc retry`, `hermes oco retry`)

Single tool that handles three cases:

- `FAILED` agent: restores `phase_before_failed` (stamped at every
  FAILED transition), clears `last_error`, `last_tick_error`,
  `consecutive_tick_failures`, `consecutive_aborts`.
- `NEEDS_INTERVENTION` agent: restores `phase_before_intervention`,
  clears the intervention fields.
- Any other non-terminal agent: resets `phase_retry_count` and
  `last_tick_error` so the next tick runs clean. Useful for stuck
  agents after a gateway restart or transient network outage.

Refuses on `DONE` / `KILLED` / `CANCELLED`, and on `FAILED` with
`last_error` containing `project gone` (truly unrecoverable;
project must be re-added first).

Auto-stamping: when `_runtime.agents.update(..., phase="FAILED")`
fires, also pass `phase_before_failed=<current phase>`. The
ergonomic helper is to set it explicitly at the call site since
not every FAILED-bound transition is resumable.

### Serve-down grace window (v0.19.1+)

The supervisor's `_serve_watchdog_loop` fires `serve_down` /
`serve_recovered` notifications and attempts exponential restarts of
`opencode serve` when it goes unreachable. During those windows
every active agent is hitting transport-level errors on its next
tick (`ConnectError` on `wait_idle`, `ReadError` on
`list_questions`, etc.) and the 3-strike tick-failure escalation
would otherwise FAIL all active agents for the supervisor's own
restart cycle.

v0.19.1 gates `_record_tick_failure`'s counter increment + escalation
on `_serve_is_unhealthy_or_healing()`, which returns True when:

- `_serve_down_notified_at != 0.0` (currently down per the watchdog), OR
- `_serve_recovered_at` is within `SERVE_HEALING_GRACE_SEC = 30 s`
  of now (just recovered, agents have not yet gotten one clean tick)

When True, the tick error is recorded (`last_tick_error`,
`last_tick_error_at`) and ONE `tick_error` notification fires per
agent per unhealthy episode (deduped via
`_unhealthy_tick_notified_agents`), but:

- `consecutive_tick_failures` is NOT incremented
- escalation to FAILED is skipped

`_clear_tick_failure(agent)` (called on every successful tick)
unconditionally drops the agent_id from
`_unhealthy_tick_notified_agents` so the next unhealthy episode
notifies fresh.

Healthy stalls (serve responsive but agent unresponsive) still
escalate at the 3-strike threshold. Only transport-during-flap
errors are gated.

When adding new agent failure paths that distinguish transient
transport errors from real stalls: gate on
`_serve_is_unhealthy_or_healing()` whenever the failure mode is
"transport to opencode serve" specifically. Do NOT gate on it for
errors from external services (github CLI, git, etc.) — those have
their own retry budgets via `_handle_phase_failure`.

### Serve post-mortem (v0.20.0+)

When `opencode serve` dies (detected by ping failure or by an
explicit `_runtime.client.last_exit_info()` poll), the orchestrator
appends a structured row to `Config.serve_crashes_file` (default
`~/.hermes/plugins/hermes-opencode/serve_crashes.jsonl`).

Row shape includes `ts`, `observed_via` (`ping_failed` |
`restart_attempt_failed` | `restart_attempt_exception` |
`restart_spawn_ping_failed`), `restart_attempt_n`, `pid`,
`exit_code`, `signal_name` (e.g. `SIGKILL`), `exit_kind`,
`uptime_sec`, `log_path`, `log_tail` (last 20 lines of the dying
process's stdout+stderr), `sec_since_last_alive`, and
`agents_active` (non-terminal agent_ids at the moment of crash).

Writers:
- `_serve_watchdog_loop` on ping failure (single row per detected
  death window — gated by the existing 10 min notify cooldown)
- `_try_restart_serve_with_backoff` per FAILED attempt (one row per
  attempt — `restart_attempt_failed` for ensure_server timeouts,
  `restart_attempt_exception` for unhandled exceptions,
  `restart_spawn_ping_failed` when the spawned process bound the
  port but didn't answer `/`)

The first record per down-episode is cached in
`_last_serve_crash_info` and embedded into the next `serve_down`
notification body via `_build_serve_down_notification`, so users
see the exit reason inline without grepping JSONL.

`_prune_serve_logs()` runs from `_cleanup_loop` and keeps the
newest `Config.serve_log_retention_count` (default 50) timestamped
serve logs. Older ones are unlink'd.

`OpencodeClient.last_exit_info()` is the single source of truth for
the dying process's exit state. It tolerates:
- `_spawned is None` -> `unknown_already_reaped` (only when a PID
  was tracked previously) or `None` (never spawned).
- `poll() is None` -> `still_running`.
- `rc < 0` (POSIX negative returncode) -> `killed_by_signal` with
  `signal_name = signal.Signals(-rc).name`.
- `rc > 128` (shell-style "killed by signal N" code) -> same.
- `rc == 0` -> `clean_exit`.
- `rc != 0` (no signal) -> `nonzero_exit`.

### When adding new serve-side failure paths

- Call `_record_serve_crash(observed_via="<your-tag>",
  restart_attempt_n=...)` so the post-mortem layer captures the
  failure shape. Use a new `observed_via` value rather than
  overloading existing ones.
- If the failure has a clear operator action (e.g. "restart attempt
  failed with port-in-use"), include it in `extra={"error": "..."}`.
- DO NOT silently swallow process-management errors. The watchdog
  is the single point of restart control; bypass it and you lose
  the audit trail.

### Phase-stuck watchdog

`_phase_stuck_loop` runs every `STUCK_CHECK_INTERVAL_SEC = 60` s.
For each agent in `STUCK_WATCHED_PHASES` (transitional / short
phases only — see the constant) whose `phase_entered_at` is more
than `STUCK_WARN_SEC = 600` s ago, fires a one-shot `phase_stuck`
notification. Re-arms when the phase changes
(`last_stuck_notify_at` is cleared by `AgentStore.update`'s
phase-change branch).

Watched: `CREATED, BOOTSTRAPPING, IDLE_TASK_COMPLETE,
REVIEW_SPAWNING, REVIEW_DELIVERED, IDLE_REVIEW_ADDRESSED,
COMMITTING`.
Skipped (long-running): `EXECUTING, EXECUTOR_ADDRESSING, REVIEWING, PR_OPEN`.
Skipped (blocked): `AWAITING_HUMAN, NEEDS_INTERVENTION, RATE_LIMITED, QUEUED`.

### Differentiated abort nudges

`_check_executor_abort` (3-strike `MessageAbortedError` detector)
varies the nudge by attempt via `ABORT_NUDGE_PROMPTS`:

1. `continue`
2. `You stopped mid-task. Resume where you left off and finish the work.`
3. `[SYSTEM DIRECTIVE: HERMES-OPENCODE - RESUME] ...`

Same 3-strike escalation, but each attempt gets a different prompt
shape. After strike 3 the agent is escalated to `FAILED` with
`phase_before_failed` set so `oc_retry` works.

### When adding new failure paths

- For recoverable transport/IO failures: call `_handle_phase_failure(agent, phase_name, summary)` instead of `_runtime.agents.update(phase="FAILED")` directly. If the failure has a clear operator action, add `on_exhausted_intervene=True`.
- When escalating to `FAILED` directly (truly unrecoverable, e.g. `project gone`): stamp `phase_before_failed=<agent.phase>` so operators have the option to `oc_retry` if circumstances change.
- Add new transitional phases to `STUCK_WATCHED_PHASES` so the watchdog catches stalls.

## Pre-LLM context architecture (LOAD-BEARING, v0.19.0+)

The hermes chat LLM receives a five-block context appended to every
user message via the `pre_llm_call` hook (registered in
`__init__.py::register`). The context is appended to the USER
message, never the system prompt, so the system prompt stays
identical across turns and Anthropic / OpenAI prompt-cache prefixes
keep hitting. All blocks are suppressed when they have nothing to
say, so the cost is zero on quiet turns.

Block order matters: directives FIRST so they aren't buried under
state noise. Per `_build_pre_llm_context(session_id, user_message)`:

1. `_DISPATCHER_DIRECTIVE` (always).
2. `_build_active_agents_block()` - one line per non-terminal agent
   with phase, session_status (idle/busy/retry from
   `_sse_session_status`), `in_phase_for`, optional pr_url, plus a
   220-char tail of the SSE text buffer when available.
3. `_build_recent_events_block(session_id)` - tail of `events.log`
   filtered to entries newer than `_session_watermarks[session_id]`.
   First call per session seeds the watermark to now (so a fresh
   chat doesn't get blasted with backlog); subsequent calls advance
   it past the latest event.
4. `_build_dispatch_nudge_block(user_message)` - when
   `_looks_like_task(user_message)` returns True (imperative verb,
   not ending in `?`, under 4000 chars).
5. `_build_answer_nudge_block(user_message)` - when the user's
   reply matches an option label of any pending /question (case-
   insensitive, plus a yes/no token shortcut when exactly one
   question is pending).
6. `_build_pending_items_block()` - the v0.14.3+ pending-question
   + permission catalog (unchanged from v0.18.0).

### When adding new context blocks

- Keep them OPTIONAL (return None on the no-op case) - silence is
  the default; cost-on-quiet-turn is the invariant.
- Add a corresponding `_build_<thing>_block()` helper in
  `__init__.py`. Do NOT inline new blocks in `_build_pre_llm_context`.
- Update the block order list above when inserting.
- Tests live in `tests/test_pure_logic.py` next to the existing
  block tests (`TestActiveAgentsBlock`, `TestRecentEventsBlock`,
  `TestAnswerNudge`).
- Be defensive against test sentinels: helpers must tolerate
  `_runtime = object()` (the v0.14.3 tests use this pattern). Use
  `hasattr` / `getattr(..., None)` guards before accessing
  `_runtime.agents` / `_runtime.config`.

### Watermark mechanics

`_session_watermarks: dict[hermes_session_id, last_ts]` is in-memory
(not persisted). On hermes restart all sessions start fresh and skip
backlog. This is intentional: a multi-hour gap shouldn't dump a wall
of historical events into the next turn.

`event_loop.tail_recent_events(since_ts, limit)` reads the tail of
`Config.events_log` (the JSONL written by `_notify_event` itself),
NOT `notifications.jsonl` (the dashboard sink). events_log has
structured `{ts, kind, agent_id, project, phase, pr_url, title,
body}` rows so the context builder can filter and format without
re-parsing.

## Progress narration loop (v0.19.0+)

Optional sibling of the stuck-watchdog. Off by default; enable via
`plugins.entries.hermes-opencode.progress_narration.enabled: true`.

`_progress_narration_loop` runs every
`Config.progress_narration_interval_sec` (default 300s) inside the
supervisor's task group. For each non-terminal agent NOT in
`{AWAITING_HUMAN, NEEDS_INTERVENTION, RATE_LIMITED, QUEUED, PR_OPEN,
CREATED}`, computes `_build_narration_snippet(agent_id,
snippet_chars)` from the SSE text buffer. If the snippet differs
from `_last_narrated_snippets[agent_id]`, fires the
`progress_narration` event via `_notify_event`.

Dedupe is intentional: a stalled / paused executor won't spam
identical "still writing the auth handler" pings every 5 min. Each
distinct snippet fires exactly once.

When adding new agent phases, decide whether they should be in the
narration skip-list (long-running blocked / terminal phases) or
narrated (transitional / working phases). Conservative default: skip
unless you're sure mid-phase narration adds value.

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
