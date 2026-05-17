# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.14.3] - 2026-05-17

### Changed

- **Dispatcher discipline for the hermes chat LLM.** Two coordinated
  surfaces now tell the hermes chat session that it is a DISPATCHER
  when calling `oc_spawn` / `oc_send`, not a planner. The opencode agent
  it spawns has full authority over its own task: planning, scoping,
  file exploration, design, execution. The hermes chat LLM's job is to
  forward the human's words verbatim, nothing more.

  1. **Tool-description hardening** (`tools.py`). `oc_spawn` and `oc_send`
     descriptions explicitly forbid planning, decomposition, analysis,
     paraphrasing, file hints, prepended background, and "improving" the
     prompt. Both `prompt` and `text` parameter descriptions repeat the
     constraint. If the human's request is unclear, the chat LLM must
     ASK the human for clarification before calling the tool, never fill
     in gaps on opencode's behalf. The `description` for `oc_spawn` went
     from 154 chars (one sentence) to ~1180 chars (full authority model
     + explicit MUST-NOT list + clarification escape hatch).

  2. **`pre_llm_call` hook injects the dispatcher directive** every turn
     (`__init__.py`). The plugin already used `pre_llm_call` to surface
     pending opencode questions/permissions to the chat LLM; that path
     now also carries a short `[hermes-opencode] DISPATCHER MODE` block
     that restates the authority model. The directive always precedes
     the pending-items block when both are present, and the hook returns
     `None` only when the plugin runtime has not yet been initialized
     (so the directive is otherwise unconditional and stays visible to
     the chat LLM on every turn). Cost: ~530 chars per LLM call.

  Why both surfaces: tool descriptions are seen at tool-selection time
  but can be ignored once the LLM commits to a call; the per-turn hook
  injection keeps the rule top-of-mind across the whole conversation.
  The two surfaces use distinct wording so the chat LLM doesn't pattern-
  match-deduplicate them away.

  Non-invasive by design. Both surfaces live entirely inside the
  hermes-opencode plugin. No edits to `hermes-agent` core, no new plugin
  hook types, no new skill-auto-load mechanism (none exists in hermes-
  agent anyway, per the v0.14.3 investigation: skills are explicit-only,
  system prompts are immutable mid-session, `pre_llm_call` is the only
  plugin-owned lever that reaches the chat LLM's user-message context).

### Added

- `tests/test_pure_logic.py::TestPreLlmCallHookDispatcherDirective` (4
  cases) covers: no-runtime returns None; runtime-set-no-pending returns
  directive only; runtime-set-with-pending puts directive before pending
  block separated by a blank line; directive contains no em-dash
  (AGENTS.md anti-pattern).
- `tests/test_pure_logic.py::TestSpawnSchemaDispatcherWording` (4 cases)
  asserts `oc_spawn` / `oc_send` schemas and their `prompt` / `text`
  parameter descriptions all contain the VERBATIM / FULL authority /
  No planning / ASK the human wording. Catches future regressions if
  someone shortens the descriptions back.

## [0.14.2] - 2026-05-17

### Fixed

- **`TypeError: 'Agent' object is not reversible` crash** in `_phase_executing`,
  `_phase_executor_addressing`, and `_awaiting_input_blocks_review` — the
  three call sites for the v0.14.0 SSE-buffer-first last-assistant-text
  lookup. A pre-existing `_last_assistant_text(items: list[dict])` helper
  (from v0.3.0, used by `_phase_reviewing` to extract reviewer-session
  text from already-fetched items) was shadowed by the new
  `async def _last_assistant_text(agent: Agent)` I added in v0.14.0.
  Python kept the later definition, so the three async callers ended up
  invoking the sync items-based function with an `Agent` object — which
  promptly tried `for item in reversed(agent)` and crashed. The
  awaiting-input gate, the pending-question notify body context, and the
  cascade entry-point all failed every tick, leaving agents stuck in
  `EXECUTING` forever. v0.14.1's `_wrap_transport_errors` was working
  exactly as designed — but THIS error was a `TypeError`, not an
  `OpencodeError`, so it propagated up to `_agent_loop`'s outer
  `except Exception` and looked identical to the v0.14.1-fixed
  transport leak.

  Fix: renamed my v0.14.0 function to `_fetch_last_assistant_text` to
  make the side-effect (HTTP fetch + SSE buffer read) explicit and
  avoid the name collision with the pre-existing pure-function
  `_last_assistant_text(items)`. All three async call sites updated.
  Both helpers now have distinct purposes:
  - `_last_assistant_text(items: list[dict])` — sync, pure, extracts
    text from an already-fetched message-items list (reviewer flow).
  - `_fetch_last_assistant_text(agent: Agent)` — async, side-effectful,
    reads the SSE text buffer first then falls back to
    `client.get_messages` (awaiting-input flow).

  Regression test added to lock the rename in: asserts both helpers
  exist with the right async/sync signature and that
  `_fetch_last_assistant_text(agent)` returns `""` without iterating
  the agent when `_runtime is None`.

## [0.14.1] - 2026-05-17

### Fixed

- **Event loop now actually recovers from opencode connection errors.**
  Pre-existing bug uncovered when opencode-serve crashed mid-session: every
  transport method (`wait_idle`, `list_questions`, `list_permissions`,
  `get_messages`, `create_session`, `send_message`, `send_message_async`,
  `delete_session`, `reply_question`, `reject_question`, `reply_permission`)
  could raise raw `httpx.ConnectError` / `httpx.HTTPError`, but every phase
  handler only caught `OpencodeError`. The raw httpx error escaped, hit
  `_agent_loop`'s generic `except Exception`, dumped a 30-line traceback to
  `errors.log` every minute, and the agent just sat there in
  `EXECUTING`/`EXECUTOR_ADDRESSING` forever with the user no wiser. Fix:
  new `_wrap_transport_errors` decorator wraps every transport method and
  reraises `httpx.HTTPError` (covers `ConnectError`, `ConnectTimeout`,
  `ReadTimeout`, `RemoteProtocolError`, `WriteError`, `NetworkError`, etc.)
  as `OpencodeError` with the original exception preserved as `__cause__`.
  Phase handlers' existing `except OpencodeError` branches now actually
  catch the failure and back off gracefully.

- **Watchdog cold-start gate removed.** Previously
  `_serve_watchdog_loop` required `_serve_seen_alive == True` ONCE in the
  current process lifetime before it would ever attempt a restart. If
  hermes was restarted while opencode-serve was already dead — exactly
  the scenario seen in production at 20:25 today — the watchdog's first
  ping failed, `_serve_seen_alive` stayed `False`, and it sat in the
  watch-only branch forever. Two stuck agents and zero restart attempts
  in the log. Fix: watchdog now treats any "unreachable" tick as
  recoverable (regardless of `_serve_seen_alive`), fires `serve_down`
  immediately at detection (subject to the existing 10-minute cooldown),
  then attempts the exponential restart sequence when
  `auto_spawn_server=True`. When the restart succeeds — or when an
  externally-restarted opencode comes back — fires `serve_recovered` on
  the next tick.

### Added

- **`opencode serve` stderr + stdout captured to a log file.** Was
  `stdout=DEVNULL, stderr=DEVNULL`, meaning every opencode crash was a
  silent black box. Now written to
  `~/.hermes/plugins/hermes-opencode/logs/opencode-serve.<YYYYMMDD-HHMMSS>.log`
  (one file per spawn). `ensure_server()` now takes an optional
  `log_dir` parameter; all in-tree callers (`tools._ensure_server`,
  `make_regen_bootstrap`, `make_regen_cleanup`, the watchdog's
  `_try_restart_serve_with_backoff`) pass `rt.config.logs_dir`. When a
  serve spawn exits during startup, the error message includes the
  tail of the new log so the root cause is in the immediate failure
  payload instead of buried under `journalctl`.

- **`serve_recovered` event** parallel to the existing `serve_down`
  event. Fires once when the watchdog observes opencode transition from
  down to alive (including externally-restarted instances). Hits the
  same `("cli","dashboard","gateway")` sink list — your DM channel
  gets a `✓ opencode serve recovered` follow-up after every outage.

- **Per-agent tick-failure tracking on `Agent`.** New fields
  `last_tick_error`, `last_tick_error_at`, `consecutive_tick_failures`
  (all migration-tolerant defaults). `_agent_loop` records the error
  name + first 200 chars of `repr(exc)` on each tick failure and
  clears the counter on success. Surfaced in `/oc list` as a `↻ N tick
  fails` chip on the agent line and as a `tick error: ...`
  continuation when the streak reaches ≥ 3. `/oc doctor` adds a
  `tick failing · <agent_id>` section enumerating every agent
  currently in a failure streak. No more "phase=EXECUTING with
  silent connection refused" mystery.

### Changed

- `AgentStore.update(field=None)` now actually clears the field
  instead of silently no-op-ing. The previous `if v is not None` skip
  was load-bearing for no caller in this codebase (verified via grep)
  and blocked the tick-failure-clearing path. Existing
  `update(... last_error=None)` calls (e.g. the
  `_phase_executor_addressing → COMMITTING` recovery in v0.5.x) now
  also work as their original authors expected.

## [0.14.0] - 2026-05-17

### Fixed

- **Reviewer no longer races into incomplete work.** Before, the
  `_phase_executing` gate transitioned to `IDLE_TASK_COMPLETE → REVIEW_SPAWNING`
  the moment the executor went idle with a non-empty worktree diff —
  including when the executor had just emitted a plain-text "which option do
  you prefer?" prompt with no formal `/question` entry. The reviewer would
  then review partial work; if it LGTM'd, the executor would commit and open
  a PR for incomplete changes. New gate: after the diff check, run the
  awaiting-input classifier on the last assistant message. If the
  classifier (or its regex fallback) says the executor is awaiting human
  input, stay in `EXECUTING` and fire an `awaiting_human` notify with
  context. Same gate applies to `_phase_executor_addressing` before
  `COMMITTING`.

- **Permission requests now fire `awaiting_human` notifications.**
  Previously `_maybe_notify_new_questions` only fanned out for `/question`
  entries — pending `/permission` entries silently stalled the agent
  without any DM. Renamed to `_maybe_notify_new_pending`; renders both
  questions and permissions with separate dedup-id sets, and prepends the
  last assistant text (≤500 chars) as a `Context:` block so the user sees
  the why alongside the prompt.

### Added

- **Awaiting-input cascade detector** ([awaiting_input.py](opencode-orchestrator/awaiting_input.py)).
  Three-layer classifier that decides whether the executor's most recent
  assistant message is waiting on a human reply when there's no formal
  `/question` or `/permission` entry:
  1. **Regex layer** (always on, free): 10 patterns covering trailing `?`,
     "which option", "should I", "would you prefer", "let me know",
     "please confirm", "y/n", explicit "awaiting your input", labeled-option
     enumeration ("Option A:", "Option B:").
  2. **LLM layer** (configurable, off-by-default disable supported): calls
     `agent.auxiliary_client.async_call_llm(task=cfg.classifier_task_name, ...)`.
     Users pick their model by adding
     `auxiliary.hermes_opencode.awaiting_input.{provider,model}` to
     `~/.hermes/config.yaml` — works with Anthropic, OpenAI, Gemini,
     OpenRouter, or any other provider hermes-agent routes. Falls back
     gracefully when the auxiliary client isn't reachable, times out, or
     emits unparseable output.
  3. **Stalled-idle reminder loop**: per-agent
     `last_awaiting_notify_at` tracks when we last DM'd. The reminder loop
     ticks every 60s; if an `EXECUTING` / `EXECUTOR_ADDRESSING` agent has
     been awaiting input for `awaiting_input.reminder_interval_sec` (default
     1800s = 30min), re-notify with elapsed time and the cached
     last-assistant snippet. Dedup-safe — won't spam.

- **Initial-prompt system directive.** `tools.py::make_spawn` now wraps the
  user's verbatim initial prompt with a `[SYSTEM DIRECTIVE: HERMES-OPENCODE
  - ORCHESTRATOR RULES]` ... `[END SYSTEM DIRECTIVE]` block instructing the
  executor to (a) use the `/question` API for human input (so the cascade
  detector is the safety net, not the primary path) and (b) emit
  `PR_OPENED:` when opening the PR. The user's prompt body remains
  bit-for-bit unchanged below the directive; the format mirrors OMO's
  `[SYSTEM DIRECTIVE: OH-MY-OPENCODE - ...]` convention so OMO's
  directive parser does not collide.

- **`Agent` carries** `last_progress_at`, `last_awaiting_notify_at`, and
  `last_classifier_verdict` (all migration-tolerant defaults). Surfaced in
  `/oc doctor` so the user can see which agents are awaiting input and the
  detector verdict that flagged them.

### Changed

- `Config` gains `classifier_*` (enabled, task name, max input/output,
  timeout) and `awaiting_input_*` (stall timeout, reminder interval)
  fields. All have safe defaults; users override under
  `plugins.entries.hermes-opencode.{classifier,awaiting_input}.*` in
  `~/.hermes/config.yaml`.

- `/oc doctor` adds three sections: `classifier`, `awaiting input`, and
  per-agent `awaiting · <agent_id>` lines for any agent with a recent
  awaiting-input notification.

## [0.13.0] - 2026-05-17

### Fixed

- **Gateway notify sink now actually delivers.** `notify._send_gateway`
  previously called `platform_registry.create_adapter(...)`, which
  returned `None` in every observed runtime because the registry's
  `_entries` factory table isn't populated by anything in our load
  path. Real notifications (`agent_done`, `pr_opened`, `cancelled`,
  `serve_down`, heartbeat) silently failed the gateway sink and only
  the dashboard JSONL appended; users with `notify_sinks=["gateway",
  "dashboard"]` thought their DM channel was working when it wasn't.
  The fix: a new `notify._resolve_live_adapter(platform_enum)` helper
  reads `gateway.run._gateway_runner_ref().adapters` directly — the
  already-instantiated adapter dict the gateway uses to send and
  receive messages. `_send_gateway` tries the live runner first, falls
  back to `create_adapter` for CLI / out-of-gateway-process contexts,
  and emits an explicit `"create_adapter returned None ... and no live
  runner found (notify is firing outside the gateway process)"` when
  both fail so the next failure is easy to triage. The `NotifyResult`
  detail now also names which path delivered the message (`live
  runner` vs `create_adapter`).

- **Removed dead `hermes send-message` subprocess fallback** from
  `_gateway_send` in `__init__.py`. `hermes send-message` was never a
  real subcommand (verified against `hermes -h`); the branch only ever
  logged a warning and dropped the echo on the floor. `_gateway_send`
  now resolves the adapter via the same shared
  `notify._resolve_live_adapter` helper as the notify path — one
  adapter-resolution path for the whole plugin.

### Added

- **`/oc test-notify [message ...]` subcommand.** Forces a full
  notify fanout (gateway DM + dashboard + cli) with a synthetic event
  and prints a per-sink `[ok]` / `[FAIL]` line with the failure detail
  for each. Intended for verifying the gateway DM trigger end-to-end
  without waiting for a real agent state transition; also useful for
  debugging future notify regressions. Also wired into the dispatcher,
  the `/oc help` text, and behind the gateway slash-command dispatch
  hook so it works from iMessage / Telegram / Discord / Slack.

### Changed

- `register_command("oc", description=...)` text now mentions `cancel`
  (was stale — listed only `list / attach / questions / doctor`).

## [0.12.1] - 2026-05-17

### Fixed

- **`OpencodeClient.ensure_server` is now thread-safe.** Completes the
  v0.12.0 watchdog feature. The lock + reaper landed in `transport.py`
  but weren't committed alongside the rest of the watchdog code in
  v0.12.0. Added `self._spawn_lock = threading.Lock()` on init; the
  entire spawn sequence in `ensure_server` is now wrapped in the lock,
  re-checks the port after acquiring, and reaps any tracked-but-dead
  `Popen` via `_reap_tracked_spawn` (terminate → 5s wait → kill → 2s
  wait) before starting a fresh process. Without this, tool-handler
  threads (`oc_spawn` on the hermes main thread) and the watchdog
  thread (via `asyncio.to_thread`) could both observe the port closed
  and race-spawn duplicate `opencode serve` processes — exactly the
  zombie-process pattern we saw in `ps aux` earlier today (3 opencode
  servers on different ports during one hermes session).

## [0.12.0] - 2026-05-17

### Added

- **`opencode serve` watchdog loop.** A new background task on the
  plugin's singleton asyncio loop pings `opencode serve` every 15 s. If
  the server was previously seen alive and then becomes unreachable, the
  watchdog attempts up to **5 restarts with exponential backoff**
  (1 s, 2 s, 4 s, 8 s, 16 s before each successive attempt — ~31 s total
  if every attempt fails) via `OpencodeClient.ensure_server`. On
  successful recovery the notification cooldown resets and tool handlers
  resume against the same server URL.
- **Critical alert on all channels when restarts are exhausted.** If all
  5 exponential restart attempts fail, a `serve_down` event fans out to
  every notification sink — `cli`, `dashboard`, and `gateway` —
  regardless of the user's `notify.sinks` config, because a dead opencode
  server stalls every agent. The fanout is throttled to once per 10 min
  while the server stays down; recovery resets the cooldown immediately.
  The event is also appended to `events.log` with `kind=serve_down`,
  `server_url`, and `attempts` metadata so the dashboard's events feed
  and `hermes oco doctor` can surface it.

### Changed

- **`OpencodeClient.ensure_server` is now thread-safe.** A new
  `_spawn_lock` (threading.Lock) guards the spawn region so concurrent
  callers — the synchronous tool handlers running on the hermes main
  thread and the asynchronous watchdog running via `asyncio.to_thread`
  in the bg event-loop — can't both spawn duplicate `opencode serve`
  processes against the same port. Before respawning, any tracked-but-
  dead `Popen` is reaped via `_reap_tracked_spawn` (terminate → 5 s wait
  → kill) so a crashed prior process can't linger as a zombie.

### Internal

- New constants in `event_loop.py`: `SERVE_WATCHDOG_INTERVAL_SEC`,
  `SERVE_RESTART_MAX_ATTEMPTS`, `SERVE_RESTART_BACKOFF_BASE_SEC`,
  `SERVE_DOWN_NOTIFY_COOLDOWN_SEC`, `SERVE_DOWN_NOTIFY_SINKS`.
- New helpers: `_compute_serve_restart_delay(attempt, base)` (pure;
  clamps non-positive attempts to 1), `_serve_watchdog_loop()` (arming +
  detection + recovery + cooldown), `_try_restart_serve_with_backoff()`
  (gated on `auto_spawn_server`), `_build_serve_down_notification()`
  (returns `(title, body, meta)`; tested), and `_notify_serve_down()`.
- `_supervisor` now spawns the watchdog alongside `_pruner_loop`,
  `_heartbeat_loop`, and `_cleanup_loop`; all four are cancelled together
  on `event_loop.stop()`.
- 8 new tests in `test_pure_logic.py` cover the exponential delay
  sequence (1, 2, 4, 8, 16 s), zero/negative-attempt clamping, custom
  bases, the `MAX_ATTEMPTS == 5` invariant, the all-three-sinks fanout
  target, and the `serve_down` notification body (server URL + attempt
  count + metadata).

## [0.12.0] - 2026-05-17

### Added

- **`CANCELLED` phase** (terminal) for tasks wound down without merging.
  Distinct from `KILLED` (which erases the agent record). Cancelled rows
  stay in `agents.json` for audit, render with the `🚫` glyph in
  `/oc list` and the dashboard, carry an optional
  `cancellation_reason`, and are archived after 12 h via the same path
  as DONE.
- **`oc_cancel` tool / `/oc cancel <agent_id> [reason ...]` slash /
  `hermes oco cancel <agent_id> [--reason ...]` CLI.** Runs the full
  cleanup sequence (delete opencode sessions, teardown reviewer
  worktree, run cleanup skill, remove executor worktree) and flips
  phase to `CANCELLED` with the supplied reason. Refuses on
  already-terminal agents (`DONE`, `KILLED`, `CANCELLED`).
- **Auto-cancel on upstream PR closed without merge.**
  `event_loop._phase_pr_open` now branches on
  `pr_state == "CLOSED"` in addition to `"MERGED"`. The agent
  transitions to `CANCELLED` with `reason="PR #N closed without merge"`
  and the cleanup helper runs end-to-end. Latency: at most
  `PR_POLL_SEC = 5 min` between the user closing the PR on GitHub and
  the local agent flipping; `oc_cancel` is the instant manual path.
- **`cancelled` event** added to the default `notify.events.enabled`
  set so users see cancellations on their DM channel.
- **Auto-detected DM channel.** `Config.from_plugin_entry` now scans
  `os.environ` for `<PLATFORM>_HOME_CHANNEL` in priority order
  (`bluebubbles, telegram, discord, slack, teams, google_chat, feishu,
  wecom, line, irc, mattermost, sms, qqbot`) and populates
  `notify_gateway_platform` + `notify_gateway_chat_id` from the first
  match — no plugin-entry config required. When a home channel is
  detected, default `notify.sinks` flips from `["cli", "dashboard"]`
  to `["gateway", "dashboard"]` so notifications land on the user's
  DM by default. Explicit `notify.gateway.platform` or
  `notify.sinks` in the plugin entry always win.
- **`/oc doctor`** now prints `notify discovery` showing where the
  gateway target came from (`explicit`, `env:<VAR>`, or absent when
  unset).
- **Dashboard** renders the `🚫` glyph for CANCELLED with a muted,
  strikethrough phase label.

### Changed

- **`event_loop.TERMINAL_PHASES`** now includes `CANCELLED`.
- **Pruner** archives both `DONE` and `CANCELLED` agents after 12 h.
- **MERGED + CLOSED branches** in `_phase_pr_open` now share a single
  `_cleanup_worktrees(agent, worktree)` helper. The MERGED branch
  behaviour is unchanged.
- **`oc_kill` tool description** clarified to direct users to
  `oc_cancel` when they want to preserve the audit record.

### Internal

- `Agent.cancelled_at: float | None` + `Agent.cancellation_reason: str | None`
  added to the dataclass (migration-tolerant — old rows load with defaults).
- `Config.notify_discovery_source: str | None` records where the gateway
  target was resolved from. Surfaced in `/oc doctor`.
- New helper `config.discover_home_channel() -> tuple[platform, chat_id, source] | None`.
- New helper `event_loop._cleanup_worktrees(agent, worktree)` — shared
  by MERGED and CLOSED branches of `_phase_pr_open`.
- New tool count: 21 (was 20).
- 25 new tests across `test_pure_logic.py`, `test_registries.py`,
  `test_commands.py`, `test_cli.py` cover: CANCELLED round-trip + the
  pruner-archives-CANCELLED rule, `_phase_pr_open` branching on OPEN /
  MERGED / CLOSED, home-channel auto-detection priority + explicit
  override / sinks-override / discovery-source labelling, the
  `/oc cancel` parser and slash handler, the `hermes oco cancel` CLI
  including the already-DONE refusal. 175/175 green.

## [0.11.0] - 2026-05-17

### Added

- **`oc_project_regenerate_cleanup` tool.** Generates (or regenerates)
  ONLY the per-project cleanup skill, leaving the bootstrap skill
  untouched. Useful for projects registered before cleanup-skill support
  landed (v0.6.0 and earlier) where `Project.cleanup_skill` is still
  `None`, or when the bootstrap has been hand-edited and you want a
  fresh cleanup that reverses it without re-running the full bootstrap
  generation. Backed by a new `bootstrap.generate_cleanup_skill(...)`
  helper that reads the existing bootstrap (if any) and the repo, then
  writes only `~/.hermes/skills/hermes-opencode__<abbrev>-cleanup/SKILL.md`
  and updates `Project.cleanup_skill` in the registry.

### Changed

- **Flag renamed to `--archived`.** `/oc list --archived` and
  `hermes oco list --archived` are now the documented form. `--all` and
  `-a` remain accepted as aliases so anyone who picked up the v0.10.0
  hint string keeps working.
- The `no agents tracked` hint when everything visible is archived now
  reads `(use --archived to include archived)`.

### Internal

- `bootstrap.generate_cleanup_skill(client, project, throwaway_worktree, registry)`:
  scoped opencode round-trip that asks for ONLY a
  `CLEANUP_BEGIN/CLEANUP_END` block; handles empty-block no-op case
  (writes a no-op skill rather than skipping registry update); returns
  typed `BootstrapResult` matching the existing surface.
- New tool count: 20 (was 19). `provides_tools` in `plugin.yaml` now
  also lists `oc_output` which was previously omitted.
- 4 new tests in `test_spawn_auto_bootstrap.py` cover: cleanup-only
  generation persisting to registry + writing the SKILL.md file, the
  bootstrap-skill-unchanged invariant, the empty-block no-op path, and
  the missing-block error path.

## [0.10.0] - 2026-05-17

### Added

- **Archived agents.** DONE agents older than 12 h are now marked
  `archived=True` in `agents.json` (still on disk, just hidden) instead of
  being hard-deleted after 4 h. The previous `_archive_done(...)` history
  append is retained for audit; what changes is that the row stays in
  `agents.json` so the dashboard / CLI can still surface it on demand.
- **`/oc list --all`** and **`hermes oco list --all` / `-a`** to include
  archived agents in the listing. Default behaviour hides them. When all
  visible agents are archived, `/oc list` returns a hint that `--all` is
  required.
- **Dashboard archived toggle.** New checkbox in the Opencode Agents
  header (`show archived`) toggles the `?include_archived=1` query string
  on `/api/plugins/hermes-opencode/agents` and on the events WebSocket.
  Archived rows render with a muted `archived` badge next to the agent
  id. The header surfaces the hidden-count when archived items are
  filtered out.
- **Executor-driven PR open** (`reviewer.executor_open_pr`). After
  reviewer LGTM (or cycle exhaustion), the plugin no longer shells out
  to `gh pr create --fill` itself. Instead it sends a structured prompt
  to the executor's existing opencode session asking it to commit any
  pending diff, push the branch, and run `gh pr create` with a concrete
  title + summary; the executor emits a `PR_OPENED: <url>` sentinel line
  that the plugin parses. Falls back to the plugin-driven
  `finalize_and_open_pr(...)` path when extraction fails. Two
  consequences: (a) PR titles + bodies are now written by the agent with
  full task context instead of a slugified agent id + verbatim prompt
  dump, and (b) the PR commit lands under the user's normal git identity
  because the executor's shell tool inherits it.

### Fixed

- **Commits no longer hard-forced to the `hermes-opencode@local` git
  identity.** `reviewer.stage_reviewer_worktree` previously passed
  `-c user.email=hermes-opencode@local -c user.name=hermes-opencode` on
  the pre-review staging commit, which made every PR show the executor's
  attribution as the plugin instead of the user's configured git
  identity. The staging commit now runs without those overrides; if
  git has no user configured it surfaces a typed `GitError` instead of
  silently swapping the author.
- **Ambiguous review verdicts no longer silently loop in `REVIEWING`.**
  When the reviewer emits neither `REVIEW: LGTM` nor
  `REVIEW: REQUESTS_CHANGES`, the classifier returns `ambiguous`;
  previously `_handle_review_text` did nothing in that branch, leaving
  the agent stuck. The same code path that handles `requests_changes`
  now runs for `ambiguous`, with the full reviewer text forwarded to the
  executor and normal cycle accounting applied.
- **Tightened reviewer prompt.** The reviewer is now explicitly required
  to run `git diff origin/<base>...HEAD`, read every changed hunk, and
  reject LGTM on untested behaviour changes, dead code, silently
  swallowed errors, placeholders, or scope creep. Concrete file+line
  citations are mandatory on any non-LGTM verdict. Replaces the prior
  generic prompt that under-specified the bar for LGTM and contributed
  to rubber-stamp reviews on shallow plan-agent passes.
- **`/oc list` PR url visibility.** The PR url (when set) is now on the
  primary line of each agent block instead of a separate continuation
  line tucked under PR_OPEN / DONE. iMessage / Slack / Discord render
  the url as a tap-target without needing to expand the row. The PR
  number-only fallback (`PR #N`) still applies when only `pr_number` is
  known.

### Internal

- `Agent.archived: bool = False` + `Agent.archived_at: float | None`
  added to the dataclass. Old `agents.json` rows missing those keys load
  with the defaults (verified by test
  `test_old_rows_without_archived_field_load_with_default`).
- `event_loop._pruner_loop` now archives instead of deleting; the
  constant `DONE_RETENTION_SEC = 4 * 3600.0` is replaced by
  `ARCHIVE_AFTER_SEC = 12 * 3600.0`.
- `event_loop._phase_committing` calls
  `reviewer.executor_open_pr(...)` first and only falls back to
  `reviewer.finalize_and_open_pr(...)` when no PR url is extracted.
- New helpers in `reviewer.py`: `executor_open_pr_prompt(branch, base)`,
  `parse_pr_opened(text)`, `executor_open_pr(client, agent, base, *, timeout_sec)`.
- 15 new tests across `test_commands.py`, `test_pure_logic.py`, and
  `test_registries.py` covering archived filtering, the `--all` flag,
  PR-url primary-line promotion, `parse_pr_opened` extraction, the
  executor-PR prompt invariants, and the migration-tolerance for old
  agent rows.

## [0.9.1] — 2026-05-17

### Fixed

- **`/oc` slash command now works through the gateway** (iMessage,
  Telegram, Discord, Slack, etc.). The `register_command(...)` call alone
  was sufficient for the CLI dispatch path but the gateway never reached
  the plugin's command handler. Pattern lifted from `eng-task-system`:
  also register a `pre_gateway_dispatch` hook that:
    1. Inspects each incoming gateway message
    2. Returns immediately for anything that isn't `/oc` followed by EOS
       or whitespace (so `/oc-list`, `/oclist`, and unrelated text pass
       straight through)
    3. For `/oc …` messages, calls the existing `make_oc_dispatcher`
       handler and echoes the result back via the channel's adapter
       (in-process gateway runner first, `hermes send-message`
       subprocess as fallback)
    4. Returns `{"action": "skip", "reason": "/oc handled inline"}` so
       the rest of the gateway's command-resolution path doesn't fire
- AGENTS.md updated with a `gateway slash-command dispatch` section
  documenting the dual-registration requirement so future contributors
  know to register both `ctx.register_command` AND a
  `pre_gateway_dispatch` filter when adding new slash commands.

## [0.9.0] — 2026-05-17

### Added

- **`/oc doctor` slash command** — single-message plugin health report.
  Lists plugin version, state directory, opencode server url, bg event-
  loop liveness, project / agent / pending counts, notify sink + gateway
  target + events config, heartbeat schedule, binary presence (opencode,
  gh, git, bun), Python dep presence (httpx, httpx-sse, yaml), state-file
  sizes, and a tail of the last few `events.log` entries. Designed to
  produce a single paste-able report for triaging.
- **Cleanup loop running every 12 hours.** New `_cleanup_loop` task on
  the bg event-loop performs four housekeeping passes:
  - truncate `events.log` to the last 5000 lines
  - truncate `notifications.jsonl` to the last 1000 lines
  - drop `history.jsonl` entries older than 30 days (based on
    `archived_at` / `done_at`)
  - remove orphan worktrees under `wt/` whose name no longer matches any
    live agent's filesystem slug
- **Per-project cleanup skill, auto-generated alongside bootstrap.** When
  `bootstrap.generate_bootstrap_skill` runs for the first time on a new
  project it now asks the opencode introspection session to emit BOTH a
  bootstrap block AND a matching cleanup block that inverses the
  bootstrap's side effects (stop docker compose services started by
  bootstrap, drop ephemeral databases, remove generated `.env` files,
  etc.). The cleanup script is written to
  `~/.hermes/skills/hermes-opencode:<abbrev>-cleanup/SKILL.md` and the
  cleanup-skill reference is stored on the project record as
  `cleanup_skill`. If the introspection session emits an empty cleanup
  block (nothing worth reversing), no cleanup skill is written and the
  field stays `None`.
- **Cleanup skill runs before worktree removal** in two places:
  - `_phase_pr_open` when the PR merges and the agent transitions to
    `DONE`. Failure of the cleanup is logged but doesn't block worktree
    removal — teardown is best-effort.
  - `oc_kill` when `remove_worktree=True`. Cleanup failure is recorded
    in the tool result's `errors` field but doesn't block teardown.

### Changed

- **`/oc list` output reformatted from a column-aligned ASCII table to
  flow-oriented per-agent lines** — works equally well in CLI, iMessage,
  Slack, and Discord (none of which render fixed-width ASCII tables
  consistently). Pattern lifted from `eng-task-system`:
    `<glyph> <agent_id> · <PHASE> · <age> [· PR #N]`
  with an indented continuation line for `FAILED` (error), `PR_OPEN`
  (url), and `DONE` (merged + url). Phase glyphs:
  ▶ executing · ⏸ idle · 🔎 reviewing · 💾 committing · 🔗 PR open ·
  ✓ done · ✗ failed · 🛑 killed.

### Notes

- The cleanup skill is only AUTO-generated when the plugin spawns the
  introspection session itself (i.e. on first `oc_spawn` for a project
  with no `bootstrap_skill`). Projects whose bootstrap skill was
  configured manually have `cleanup_skill=None` until you either
  (a) run `oc_project_regenerate_bootstrap` (which regenerates both),
  or (b) manually write
  `~/.hermes/skills/hermes-opencode:<abbrev>-cleanup/SKILL.md` and call
  `oc_project_set_repo_path` or edit `projects.json` to set the field.

## [0.8.0] — 2026-05-17

### Changed (breaking)

- **Project renamed from `opencode-orchestrator` to `hermes-opencode`.**
  Matches the convention used by other hermes plugins (`hermes-achievements`,
  `hermes-claude-auth`, etc.) and makes the install command read naturally:

      hermes plugins install that-ambuj/hermes-opencode

  All `opencode-orchestrator` references in code, manifests, JSON / YAML
  config keys, dashboard mount paths, install symlink targets, and skill
  namespaces become `hermes-opencode` (kebab) or `hermes_opencode` (snake).
  Tool prefixes (`oc_*`), slash command (`/oc`), and CLI subcommand
  (`hermes oco`) stay unchanged — those refer to *opencode* (the agent we
  drive), not the plugin name.

  **Migration for existing installs:**

      mv ~/.hermes/plugins/opencode-orchestrator ~/.hermes/plugins/hermes-opencode

  Then in `~/.hermes/config.yaml`, rename the key:

      plugins:
        entries:
          opencode-orchestrator:  →  hermes-opencode:
            …

  And replace `plugins.enabled: [opencode-orchestrator]` with
  `[hermes-opencode]`. The GitHub repo was renamed via `gh repo rename`
  which keeps redirects, so old install URLs continue to resolve.

### Cleaned up

- README "Roadmap" section replaced with a "Status" table marking each
  shipped surface ✓ against the release it landed in. The intro paragraph
  now opens with a one-line elevator pitch and the canonical
  `hermes plugins install …` snippet.
- README "Requirements" section expanded to match the omo plugin's
  table-with-glyphs format: hermes-agent, opencode binary, `gh` CLI,
  git ≥ 2.40, Python deps. Each item links to its source.

## [0.7.0] — 2026-05-17

### Changed (breaking)

- **Slash commands consolidated to a single `/oc` with subcommands.** The
  prior `/oc-list`, `/oc-attach`, `/oc-questions` commands are gone.
  Equivalents:
    `/oc-list`         →  `/oc list`
    `/oc-attach …`     →  `/oc attach …`
    `/oc-questions`    →  `/oc questions`
  Running `/oc` with no args (or `/oc help` / `/oc --help`) prints a help
  message listing all subcommands. Unknown subcommands surface the help
  inline. Subcommand names are case-insensitive.

  Rationale: the user-facing CLI subcommand is already `hermes oco
  {list,attach,kill,projects}` and matching the slash-command shape to it
  is the natural pattern. Single registration also means just one slot in
  `hermes plugins list`'s slash-command count.

## [0.6.0] — 2026-05-17

### Added

- **Event-based notifications** on key state-machine transitions, not just
  the hourly heartbeat. New event kinds:
  - `pr_opened` — fires when an agent transitions to `PR_OPEN` with the PR
    URL + branch in the body.
  - `done` — fires when the PR merges and the agent transitions to `DONE`.
  - `failed` — fires on any `FAILED` transition with the captured
    `last_error`.
  - `awaiting_human` — fires when a new pending `/question` is detected
    for an agent (deduped per question_id so we don't replay on each
    polling tick).
  - `review_started` — fires when the reviewer session spawns on the
    sister worktree.
  Each event is gated by `notify.events.enabled` in the plugin config
  (default: all five kinds enabled). Events fan out to the same sinks as
  the heartbeat (`cli` / `gateway` / `dashboard`) and ALSO get logged
  unconditionally to `~/.hermes/plugins/hermes-opencode/events.log`
  as newline-delimited JSON for diagnostics.
- **`events.log` diagnostic sink** at
  `~/.hermes/plugins/hermes-opencode/events.log`. Every event the
  plugin emits is appended regardless of which user-facing sinks are
  configured, so `tail -f` on it gives a complete view even when no
  gateway DM target is set up yet. Each line is JSON:
  `{ts, kind, agent_id, project, phase, pr_url, title, body}`.

### Fixed

- **Heartbeat scheduler no longer dies silently** when `_runtime` is
  briefly `None` at startup. Previously the `_heartbeat_loop` coro did
  `if _runtime is None: return`, exiting forever if it raced the
  `start(runtime)` setter. Now it retries every 10 s until `_runtime` is
  populated. Same retry-instead-of-return guard added to `_pruner_loop`.
- **Heartbeat scheduler now logs at INFO** when it computes the next
  fire time and when it actually fires (sink results inline). Grep
  `~/.hermes/logs/agent.log` for `heartbeat:` to debug.

### Notes

- If you weren't seeing heartbeats before, set
  `oc_set_notify_target(platform="telegram", chat_id="...")` (or your
  preferred platform/chat_id) so the gateway sink has a target. The CLI
  sink only works inside an active interactive `hermes chat` session;
  the dashboard sink writes to `notifications.jsonl` (visible in the
  dashboard tab). With nothing configured the new `events.log` file
  still captures everything for `tail -f` debugging.

## [0.5.1] — 2026-05-17

### Fixed

- **`gh pr create` finding an existing PR is no longer fatal.** When the
  branch already has an open PR, `gh pr create` exits non-zero with stderr
  like `a pull request for branch "..." into branch "main" already exists:
  <url>`. Previously the orchestrator treated this as a `PrError` and
  transitioned the agent to `FAILED` even though the PR demonstrably
  existed. Now `pr.open_pr` parses the URL out of the gh output via a new
  `_existing_pr_from_output` helper, calls `pr_state` to load the live PR
  status, and returns a `PrInfo` as if `gh pr create` had succeeded. If
  the follow-up `pr_state` call itself flakes (rate-limited `gh pr view`,
  etc.), it falls back to `state="OPEN", merged_at=None` rather than
  failing the agent — the whole point of the recovery is that the PR
  demonstrably exists. Covered by `tests/test_pr.py`.

### Rebased

- Rebased onto `0.5.0` (had originally branched off PR #2 / `0.3.5` and
  proposed `0.3.6`). Patch bump to `0.5.1` since main shipped both PR #1
  (`0.4.0`) and PR #2 (`0.5.0`) in the meantime. The PR #2-derived
  changes that this branch carried (cli.py, commands.py, slash command
  registrations, etc.) are dropped — they're already in `0.5.0`. Only
  the focused `gh pr create` recovery fix remains.

## [0.5.0] — 2026-05-17

### Added

- **Three slash commands** for ops without going through the LLM:
  - `/oc-list` — pretty table of all tracked agents (agent_id, project,
    branch, phase, pr, age).
  - `/oc-attach <agent_id> [--lines N]` — print the last N lines of an
    agent's transcript (pulled via `client.get_messages`; future SSE-buffer
    integration noted).
  - `/oc-questions` — list all pending opencode questions across active
    agents, formatted with structured options when present.
- **`hermes oco` CLI subcommand** for shell-level driving outside a chat
  session: `oco list`, `oco status [agent_id] [--json]`,
  `oco attach <agent_id> [--lines N]`, `oco kill <agent_id> [--force]`,
  `oco projects`. Each subcommand instantiates `Runtime` from disk-only
  state so it works with no in-process event loop.
- **`config.load_entry_config()`** extracted from `__init__.py` so both
  `__init__.py` (in-session) and `cli.py` (out-of-session) can read the
  same `plugins.entries.hermes-opencode` config without a circular
  import.

### Rebased

- Rebased onto `0.4.0` (had originally branched off `0.3.4` and proposed
  `0.3.5`). Minor bump to `0.5.0` since the new surfaces are user-facing
  features rather than fixes.

## [0.4.0] — 2026-05-17

### Added

- **Configurable review cycles.** New `review.max_cycles` config (default `1`)
  controls how many automatic address-and-rereview rounds the executor will
  go through before transitioning straight to `COMMITTING`. Manual
  `oc_review_again` calls also bump the per-agent `review_cycle_count` so the
  cap is tracked across user-initiated retries.
- **Auto bootstrap-skill generation on first spawn.** When `oc_spawn` is
  called for a project that has no `bootstrap_skill` yet, the plugin now
  spins up a one-shot opencode introspection session in a throwaway worktree
  and generates a `SKILL.md` before the executor session starts. Gated by
  `bootstrap.auto_on_first_spawn` (default `true`).
- **`oc_output` tool + live SSE consumer.** A background per-agent task
  subscribes to opencode's `GET /event` stream and accumulates
  `message.part.delta` / `message.part.updated` text payloads into an
  in-memory buffer. The new `oc_output` tool returns the buffered text (or
  falls back to a `/message` pull when empty), with an optional `clear` flag
  to reset the buffer after read. Tool count: 18 → 19.
- **Dashboard live-events WebSocket.** New `/events` endpoint on the
  dashboard router pushes an initial snapshot plus `agents` / `heartbeat`
  deltas based on `agents.json` / `notifications.jsonl` mtime changes. The
  React bundle now opens the WebSocket on mount, falls back to the existing
  5s poll if the socket errors or closes within 5s, and shows a `ws` /
  `poll` transport indicator in the header.
- **PR title cleanup.** PR titles are now derived from the agent's task slug
  via `_pr_title_from_agent_id` — collision suffixes (`-2`, `-3`, …) are
  stripped, kebab is replaced with spaces, and the result is capitalized.
  The pre-review staging commit also uses the cleaned title
  (`chore: <title>` instead of `[wip] checkpoint before review`).

### Rebased

- Rebased on top of `0.3.4` (had originally branched off `0.3.1`). The PR
  originally hand-edited `dashboard/dist/index.js`; per the convention
  documented in `AGENTS.md`, those changes were translated into
  `dashboard/src/index.jsx` and `dist/index.js` was regenerated via
  `bun run build`. The `make_spawn` regression of the 0.3.2 fix
  (`send_message` → `send_message_async`) was also corrected before merge.

## [0.3.4] — 2026-05-17

### Added

- **Dashboard agent detail modal.** Click any agent row in the dashboard to
  open a centered detail view showing project, branch, session id, worktree
  path, reviewer session/worktree (if applicable), review cycle count, PR
  url + number + merged-at, done-at, last error, and the initial prompt in
  a scrollable code block. Dismiss with Esc or by clicking the backdrop.
  Clickable rows have a hover highlight; the PR link cell stops propagation
  so clicking the PR link doesn't also open the modal.
- **`AGENTS.md`** at the repo root: developer notes for AI agents working
  on the plugin. Covers plugin runtime contract, tool-schema convention
  (the 0.3.0 → 0.3.1 fix), sync vs async opencode endpoints (the 0.3.1 →
  0.3.2 fix), reviewer worktree isolation, atomic state writes, dashboard
  build workflow, CSS variable convention (the 0.3.2 → 0.3.3 fix), and a
  blocking-anti-patterns section.

## [0.3.3] — 2026-05-17

### Fixed

- **Dashboard text was invisible** against the host theme. CSS used variable
  names that don't exist in hermes' dashboard (`--foreground`, `--muted`,
  `--border`); the fallback colors I shipped (`#ddd`, `#888`) collided with
  whatever the host theme rendered, so text was only visible during selection.
  Now uses the actual host variables: `--color-foreground`,
  `--color-muted-foreground`, `--color-border`, `--color-card`,
  `--color-primary`, `--color-ring`, `--font-mono`. Matches the convention
  used by the bundled `hermes-achievements` dashboard plugin.

### Added

- **Dashboard refresh button** in the header row. Manually triggers a fetch
  in addition to the 5s auto-poll; spins while in-flight; shows "updated Ns
  ago" relative timestamp.
- **Proper React source + build pipeline.** Previously `dashboard/dist/index.js`
  was hand-edited vanilla `React.createElement` calls (build artifact masquerading
  as source). Now:
    - `dashboard/src/index.jsx` is the canonical source (proper JSX)
    - `dashboard/package.json` declares esbuild as a devDep and exposes
      `bun run build` (or `npm run build`) which compiles `src/index.jsx` →
      `dist/index.js` using `--jsx-factory=React.createElement
      --jsx-fragment=React.Fragment --format=iife`
    - `dashboard/dist/` is still committed (hermes loads it at runtime)
  Future dashboard work edits `src/`; `dist/` is regenerated.

## [0.3.2] — 2026-05-17

### Fixed

- **`oc_spawn` no longer blocks the hermes main session.** Previously the
  initial prompt was sent via `POST /session/:id/message`, which streams the
  full assistant turn inline before returning — blocking the hermes tool
  handler (and therefore the user's chat) for the entire first turn (often
  30 s – several minutes for non-trivial tasks). Now uses
  `POST /session/:id/prompt_async`, which queues the work and returns
  immediately. The plugin's bg event-loop picks up state transitions via the
  existing polling / SSE channels.
  Trade-off: the return value no longer carries `first_turn_assistant_text`
  or `first_turn_finish`. Use `oc_wait` + `oc_status` (or the upcoming
  `oc_output` from v0.4) to inspect the first turn's result.
  General rule for future tool handlers: any code path called synchronously
  by a hermes tool dispatcher must use non-blocking opencode endpoints. Code
  inside `event_loop._phase_*` may use blocking endpoints freely.

## [0.3.1] — 2026-05-17

### Fixed

- **Tool schemas now expose `description` and `parameters` to the LLM.** The
  original 0.3.0 release passed descriptions as a `register_tool(description=...)`
  kwarg, which hermes' registry silently drops. Schemas also lacked the
  `parameters` wrapper required by the OpenAI tool-call format. As a result the
  LLM was seeing tools by name only with empty descriptions and ill-formed
  parameter shapes. All 18 tool schemas now embed `name` + `description` +
  `parameters` inline per the convention used by `plugins/spotify/tools.py`.

## [0.3.0] — 2026-05-17

Initial public release. End-to-end orchestration of multiple opencode agents in
git worktrees, driven from a hermes-agent session.

### Added

- **Project registry** (`oc_project_add`, `oc_project_list`, `oc_project_show`,
  `oc_project_remove`, `oc_project_set_repo_path`,
  `oc_project_regenerate_bootstrap`). Project keys derived from
  `git remote.origin.url`; abbreviations auto-derived from kebab-segments.
- **Spawn / drive surface** (`oc_spawn`, `oc_send`, `oc_status`, `oc_wait`,
  `oc_kill`). Initial prompts are forwarded to opencode **verbatim**.
- **Agent naming**: `<abbrev>/<task>` ≤ 20 chars, with collision-aware
  numeric suffixes that trim the task slug to fit. Filesystem encoding via
  `/` → `__`.
- **State machine**: per-agent lifecycle CREATED → BOOTSTRAPPING → EXECUTING →
  IDLE_TASK_COMPLETE → REVIEW_SPAWNING → REVIEWING → REVIEW_DELIVERED →
  EXECUTOR_ADDRESSING → IDLE_REVIEW_ADDRESSED → COMMITTING → PR_OPEN → DONE
  (plus FAILED / KILLED). Driven by a singleton background asyncio loop.
- **Reviewer cycle** (`oc_review_now`, `oc_review_again`, `oc_skip_review`):
  reviewer runs in a **separate git worktree** (`<wt>.review/`) with
  opencode's `plan` agent to enforce read-only review. `REVIEW: LGTM` /
  `REVIEW: REQUESTS_CHANGES` tokens drive the classifier.
- **PR opener + merge poller**: `gh pr create --fill` after review;
  `gh pr view` polled every 5 min; agents auto-transition to DONE on merge.
- **Bootstrap subsystem**: shell-out of the project's bootstrap skill bash
  block, with **automatic opencode-driven recovery** on failure. Recovery
  session can also write back an updated skill if it changes the procedure.
- **Skill generation** (`oc_project_regenerate_bootstrap`): spawns a
  short-lived opencode `build` session to introspect a repo and emit
  `SKILL.md` with a fenced bash block delimited by `BOOTSTRAP_BEGIN` /
  `BOOTSTRAP_END`.
- **Question routing** via `pre_llm_call` hook: pending opencode
  questions are injected into the user message as ephemeral context so
  hermes' own LLM can decide whether to call `oc_answer` with the user's
  verbatim reply.
- **`oc_answer` tool**: replies to `/question` entries with verbatim user
  text or structured option labels; can also reject.
- **`oc_pr_status`**: live `gh pr view` for a specific agent's PR.
- **Heartbeat scheduler** (`oc_heartbeat_send_now`, `oc_set_notify_target`):
  top-of-hour reports with TZ-aware day window (`HERMES_TIMEZONE` →
  `timezone` in config.yaml → system tz). Unconditional inside
  `unconditional_hours` (default `[9, 23]`); otherwise only when there are
  pending tasks.
- **Notify fanout** (CLI `inject_message` + gateway DM + dashboard JSONL):
  each sink is opt-in via plugin config; gateway DM uses the verified
  `load_gateway_config()` + `platform_registry.create_adapter()` +
  `model_tools._run_async()` pattern.
- **Retention pruner**: DONE agents drop off the visible list 4h after
  merge, with a forensic archive at `history.jsonl`.
- **Dashboard tab** (`/opencode-agents`): FastAPI router exposing 8
  read-only endpoints (`/health`, `/agents`, `/agents/{id}`, `/projects`,
  `/projects/{label}`, `/heartbeats`, `/history`, `/`) + vanilla-JS React
  bundle using `window.__HERMES_PLUGIN_SDK__` + auth via
  `X-Hermes-Session-Token`.
- **Phase-0 transport spike** at `scripts/phase0_opencode_spike.py` and
  **Phase-1 round-trip smoke** at `scripts/phase1_smoke.py`. Both run via
  `uv run` with inline PEP-723 deps.

### Tested

- Phase-0 spike: 10/10 HTTP contracts against a real `opencode serve`
- Phase-1 smoke: 16/16 steps end-to-end (project_add → spawn → wait →
  send → kill round trip with real opencode)
- Plugin reload: 18 tools / 3 hooks registered through hermes
  `PluginManager` with `error=None`
- Heartbeat smoke: dashboard sink writes formatted JSONL
- Dashboard router smoke: all 8 endpoints return expected payloads

### Notes

- Real executor → reviewer → PR cycle requires `gh auth login` plus a
  writable git remote. The state-machine plumbing is wired and verified
  but full end-to-end PR opening is out of scope for the initial release
  smoke test.
- opencode silently resolves the requested `agent` (e.g. `"build"`) to
  the active OMO/oh-my-openagent profile (e.g. `"Sisyphus - Ultraworker"`)
  when oh-my-openagent is installed. The plugin's executor/reviewer
  distinction lives in the plugin's `agent_id` layer and is unaffected.

[0.3.0]: https://github.com/that-ambuj/hermes-opencode/releases/tag/v0.3.0
