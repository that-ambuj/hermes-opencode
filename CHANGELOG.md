# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.22.0] - 2026-05-19

Multi-use plugin shape. Until v0.21.x every `oc_spawn` was treated as a
PR-bound coding task; investigation prompts (RCA, benchmark, "explain
X") had no first-class workflow and either landed in
`NEEDS_INTERVENTION` (post-v0.21.1) or stuck forever in `EXECUTING`
(pre-v0.21.1). v0.22.0 elevates investigation to a first-class mode,
adds a read-only quick-lookup tool, and bootstraps brand-new projects
from scratch.

### New tools

- **`oc_ask <project> <prompt>`** - read-only one-shot. Opens an
  opencode session DIRECTLY in `project.repo_path` (no worktree, no
  agent record) with `agent="plan"` (opencode's read-only built-in
  agent). Hybrid blocking: returns the answer inline if it arrives
  within `timeout` (default 60s); else returns `in_flight=true` with
  an `ask_id` and fires an `ask_complete` notification when the
  answer eventually arrives (up to 600s). Opencode session is
  deleted in both success branches.
- **`oc_spawn ... mode="investigation"`** - new `mode` parameter on
  the existing `oc_spawn` tool. Investigation-mode agents skip
  REVIEW_SPAWNING / COMMITTING / PR_OPEN entirely; on idle they
  transition directly to the new terminal phase `INVESTIGATION_DONE`
  with the executor's final assistant text as the deliverable.
- **`oc_promote_to_investigation <agent_id>`** - operator escape
  hatch. For `mode="task"` spawns that finished with no diff and
  landed in NEEDS_INTERVENTION, this transitions them to
  INVESTIGATION_DONE while preserving the executor's report. Listed
  as the 4th resolution path in the no-diff intervention notification
  body, alongside `@<id> <follow-up>`, `oc_cancel`, `oc_retry`.
- **`oc_project_init <label> <path> [github_org]`** - greenfield
  bootstrap. mkdir + `git init -b <base_branch>` + seed `.gitignore`
  + `README.md` + initial commit (under user's git identity, no
  overrides) + optional `gh repo create --<visibility> --source .
  --remote origin --push` + `oc_project_add` internally. Fails fast
  if `gh` not authenticated when `github_org` is supplied. Refuses
  on duplicate project labels and on non-empty paths that aren't
  already git repos. Omit `github_org` for local-only projects.

### State machine

- New terminal phase `INVESTIGATION_DONE` (added to `state.PHASES`
  AND `state.TERMINAL_PHASES`). Pruner archives via the same 12h
  policy as DONE / CANCELLED using a new `Agent.investigation_done_at`
  timestamp.
- New `Agent.mode: str = "task"` field. Validated against
  `state.AGENT_MODES = frozenset({"task", "investigation"})`. Set at
  spawn time via `oc_spawn` `mode` param; immutable thereafter.
- New `Agent.investigation_deliverable: str | None` field stores the
  executor's final assistant text (truncated at 8000 chars).
- New `_phase_investigation_done` handler (terminal, sleep-only;
  mirrors `_phase_done`).
- New `_enter_investigation_done(agent, deliverable, source=...)`
  helper. Sources: `READY_FOR_REVIEW`, `idle-debounce`, `promote`.
  Runs `_cleanup_worktrees` before the phase transition and fires
  the `investigation_done` event.
- `_phase_executing` routes investigation-mode agents to
  `_enter_investigation_done` AFTER the upfront cascade (rate-limit,
  abort, pending Q/P, status-poll, awaiting-input). The
  awaiting-input cascade still wins: prose questions route to
  AWAITING_HUMAN exactly like task mode.

### `oc_spawn` validation

Unknown-project errors now point at BOTH `oc_project_init` (for
greenfield) and `oc_project_add` (for existing local repos). Same
error message in `oc_resume_pr` and `oc_send`.

### Notifications

- New default events: `investigation_done`, `ask_complete`. Both
  added to `Config.notify_events` default set. `investigation_done`
  body shows the trigger source and the last 2000 chars of the
  deliverable; `ask_complete` body pairs the original question with
  the answer.
- The no-diff intervention body now lists
  `oc_promote_to_investigation <agent_id>` as the 4th resolution
  path.

### AGENTS.md

New `## Agent modes (LOAD-BEARING, v0.22.0+)` section pins:
- The `task` vs `investigation` mode contract
- The `_enter_investigation_done` helper signature and contract
- The `oc_promote_to_investigation` operator escape hatch
- The `oc_ask` no-worktree read-only flow
- The `oc_project_init` greenfield bootstrap flow
- "When extending agent modes" checklist for future authors

### Tests

524 passing (was 498; +26 new across 6 classes):

- `TestAgentModeField` (5): default mode, explicit mode,
  `AGENT_MODES` constant, `INVESTIGATION_DONE` in PHASES, in
  TERMINAL_PHASES.
- `TestEnterInvestigationDone` (5): idle-debounce transitions +
  notifies, READY_FOR_REVIEW body marker, promote body marker, skips
  terminal agents, deliverable truncation at 8000 chars.
- `TestOcPromoteToInvestigation` (5): success path, refuses
  EXECUTING, refuses DONE, refuses unknown agent, refuses missing
  agent_id.
- `TestOcProjectInit` (5): local-only init creates + registers,
  refuses duplicate label, refuses non-empty non-git path, refuses
  invalid visibility, seeds README with description.
- `TestOcAskValidation` (4): refuses missing project/prompt/unknown
  project/zero timeout.
- `TestOcSpawnModeValidation` (1): unknown-project error points at
  both new and existing tools.

Plus updated `TestEnterNoDiffIntervention.test_body_lists_oc_promote_to_investigation_as_fourth_resolution`.

## [0.21.1] - 2026-05-19

Fix the EXECUTING-no-diff deadlock that left investigation-only agents
(RCA, benchmark, "explain X") stuck in `EXECUTING` forever with zero
user-visible signal. Adds an authoritative polling backstop on
`/session/status` so the cache stays accurate even when the SSE
consumer disconnects or the v2.wait endpoint no-ops.

### Bug

`_phase_executing` had two no-diff `return` statements (one after the
`READY_FOR_REVIEW` sentinel branch, one after the 120 s idle-debounce
branch). When the executor finished a task that produced no
committable changes — a pure RCA report, a benchmark write-up, a
"yes, the code is correct as-is" answer — the worktree stayed empty,
both branches silently returned, and the agent ticked forever in
`EXECUTING`. The state machine had no exit for "task done, nothing
to commit", because every other recognized terminal condition (Q/P,
abort, READY_FOR_REVIEW + diff, debounce + diff) required either a
question or a diff. The `STUCK_WATCHED_PHASES` watchdog explicitly
excludes `EXECUTING` (it's classified as "long-running by design"),
so no `phase_stuck` notification ever fired.

Live evidence: `BCK/customer-oauth` stuck for 3+ hours after a clean
RCA report; `BCK/analytics-perf` cancelled manually 6 hours earlier
with `cancellation_reason = "Exploration only — benchmark report
complete, no PR needed"` (the user's own diagnosis of the same bug).

### Fix

`event_loop._enter_no_diff_intervention(agent, last_text, source=...)`
transitions the agent to `NEEDS_INTERVENTION` via the existing
`_enter_needs_intervention(...)` helper with
`reason="executor_idle_no_diff"`. The notification body shows the
last assistant text tail and lists the three operator actions that
resolve it:

```
  @<agent_id> <follow-up>  - send a new instruction; executor resumes
  oc_cancel <agent_id>     - finish without opening a PR
  oc_retry <agent_id>      - re-tick (only useful with new context)
```

Both no-diff branches in `_phase_executing` route through the helper.
The awaiting-input cascade is checked first so a prose-question
false-positive surfaces as `AWAITING_HUMAN` (the established path)
rather than getting eaten by the intervention path.

### Polling backstop: `_refresh_status_via_poll`

New helper that calls `OpencodeClient.list_session_status(worktree)`
(the v0.21.0 SDK addition) and writes the result through to
`SessionStatusCache` with `source="status-poll"`. Per opencode's
own SessionStatus implementation, the in-memory map DELETES idle
entries, so `session_id NOT IN status_map` is the canonical
"this session is idle" indicator.

`_phase_executing` and `_phase_executor_addressing` now call
`_refresh_status_via_poll` at the top of every tick. Replaces the
implicit reliance on `_wait_idle_through_cache` (which calls the
v2.wait endpoint — a server-side `function*() {}` no-op that returns
204 in 5ms regardless of actual status). The cache is now truly
authoritative: SSE for sub-second transitions while connected,
status-poll for the source of truth on every tick. The
BCK/p-list-tests "cache stuck at busy across a serve flap" scenario
that motivated v0.20.2 is now self-healing on the very next phase
tick.

### Tests

498 passing (was 487; +11 new):

- `TestRefreshStatusViaPoll` (3): session-absent-means-idle, busy
  session writes busy to cache, retry status writes retry to cache.
- `TestEnterNoDiffIntervention` (8): transitions to
  NEEDS_INTERVENTION, fires the right notification kind, body
  includes follow-up instructions, body source marker differs by
  trigger, body includes last text tail, empty last text skips
  tail section, idempotent on already-intervened agent, skips
  terminal agent.

### Documentation

- `AGENTS.md` — two new subsections under "State machine":
  "No-diff completion (LOAD-BEARING, v0.21.1+)" pins the
  intervention contract; "Polling backstop via `/session/status`
  (LOAD-BEARING, v0.21.1+)" pins the cache-writer rule.

## [0.21.0] - 2026-05-19

Replace the hand-written `httpx` transport with the typed
[`opencode-api`](https://github.com/that-ambuj/opencode-python-sdk) SDK
(liblab-generated from opencode's OpenAPI spec, version-pinned to the
opencode server release). Drop-in migration: every public
`OpencodeClient` method keeps its existing signature and return type;
internal bodies now call SDK service methods underneath.

### Why

Each opencode-server bump used to require hand-translating new endpoints
into raw `httpx` calls. The SDK is generated from the spec, so endpoint
coverage tracks the server exactly when the tag is bumped. v0.21.0 is
the foundation for v0.21.x deadlock + narration fixes that need the new
`/session/status`, `/session/:id/todo`, `/session/:id/diff` endpoints
plus richer bus events.

### What changed

- `transport.py` rewritten — every API method now calls
  `OpencodeAsync.<service>.<method>()` instead of `httpx.AsyncClient(...)`.
  Two lazy SDK clients per `OpencodeClient` (`_sdk_default` 60s,
  `_sdk_long` 600s) so timeout sensitivity is preserved.
- `OpencodeClient.ping()` and `OpencodeClient.stream_events()` STAY on
  raw `httpx` / `httpx-sse`: ping needs a 2s timeout independent of the
  SDK client, and the SDK doesn't surface `/event` as a streaming
  iterator. Documented as intentional in AGENTS.md.
- New transport methods enabled by the SDK:
  - `list_session_status(directory)` — `GET /session/status` (the
    canonical "all idle" indicator: empty dict = every session idle).
  - `list_todos(session_id, directory)` — `GET /session/:id/todo`.
  - `session_diff(session_id, directory, message_id?)` — `GET
    /session/:id/diff`.
- Tests updated where they monkey-patched the old `_client` factory:
  `test_resilience.test_wait_idle_wraps_connect_error` now patches
  `client._sdk` to return a stub SDK whose `v2.v2_session_wait` raises
  `ConnectError`, verifying the same `OpencodeError` wrapping contract.
- `requirements.txt` adds `opencode-api @ git+https://github.com/that-ambuj/opencode-python-sdk.git@v1.15.5`.
- `wait_idle()` no longer silently swallows arbitrary `RequestError` —
  only `httpx.ReadTimeout` was swallowed in the pre-migration code, and
  the v2 wait endpoint is a server-side no-op anyway. Any transport
  failure now propagates as `OpencodeError`, restoring the original
  `_phase_executing` backoff path on transport-down.

### Wire shape preserved

Every method returns plain Python dicts/lists/bools — not the SDK's
typed pydantic-like models. Internally each helper extracts
`OpencodeResponse.raw.json()` so callers see the original camelCase
field names (`id`, `sessionID`, `projectID`) instead of the SDK's
snake_case attribute names (`id_`, `project_id`). Zero changes needed
in `event_loop`, `tools`, `bootstrap`, `commands`, `cli`, `reviewer`.

### Two upstream SDK bugs patched in the v1.15.5 tag

The SDK is liblab-generated and ships two bugs surfaced during this
migration. Both are patched in the fork and folded into the v1.15.5
tag in-place (the SDK version stays linked to the opencode server
version):

1. `OpencodeAsync.__init__` calls `super().__init__()` which sets
   access_token + timeout on the SYNC services, then overwrites every
   `self.<service>` with the async equivalent — losing both. The patch
   re-applies them at the end of `__init__`. Without it, every async
   call raises `KeyError: 'access_token_auth'`.
2. `cast_models._get_instanced_type` unconditionally coerces values to
   their declared type, including the `SENTINEL` default object that
   signals "argument not passed". For Enum-typed optional params this
   raises `ValueError: <SENTINEL> is not a valid <EnumType>`. The patch
   short-circuits when `data is SENTINEL`.

Both patches carry `# patch: ...` markers so a future regen surfaces
them in code review.

### Test count

487 → 487 (no count change; one test updated for new mock path).

## [0.20.3] - 2026-05-19

Three fixes for the human-input surface, all motivated by one live
incident on `BCK/analytics-perf` where a `/question` Request carried
3 sub-questions but only the first reached the user, the rest were
"answered" on the user's behalf by the chat LLM, and the user's
follow-up reply was queued on `prompt_async` while the executor
stayed stalled on a Deferred that never resolved.

### Bug #1: only the first sub-question reached the user

The opencode `/question` API is "one Request, N sub-questions":

```ts
class Request { id, sessionID, questions: Info[], tool? }
```

`event_loop._format_question_block(q)` was reading
`(q.get("questions") or [{}])[0]` — index 0 only. Sub-questions
2..N were silently dropped from the notification body. The chat
LLM's pre-LLM `pending items` context DID iterate all sub-questions
but emitted the same `question_id` on every line, so the LLM had
no way to address them separately.

Fix:

- `_format_question_block` now formats every sub-question with a
  `[<qid> #<idx>/<total>]` prefix and the per-sub-question options
  bullet list. Single-sub-question Requests omit the index for
  brevity (back-compat for notification body shape).
- `__init__._build_pending_items_block` annotates Requests with
  N > 1 as `(N sub-questions; answer ALL via multi_answers)`, lists
  every sub-question under its parent `question_id` with `#1 .. #N`
  labels, and emits multi_answers-shaped forwarding guidance only
  when at least one multi-sub-question Request is pending.

### Bug #1b: oc_answer could only fill the first sub-question

opencode's reply payload shape is:

```ts
class Reply { answers: Array<Array<string>> }   // string[][]
```

One outer entry per sub-question; each inner array holds the
selected option labels (or `[free_text]` for custom answers).
Missing / short outer entries surface as `Unanswered` to the
executor.

The plugin was sending a flat `list[str]` payload, which opencode's
Effect-schema decoder accepted but interpreted as "one sub-question
answered, the rest Unanswered". The executor then either guessed
based on its own defaults (matching the user's report:
"answering the rest of them on user's behalf") or re-asked the
same sub-question via a new Request.

Fix:

- `transport.OpencodeClient.reply_question` signature changed from
  `answers: list[str]` to `answers: list[list[str]]`. The docstring
  now pins the shape contract to prevent regressions.
- `tools.ANSWER_SCHEMA` adds a new `multi_answers: Array<Array<string>>`
  field. The chat LLM is instructed to use it for multi-sub-question
  Requests.
- `tools._build_reply_payload(qid, args)` is the single payload
  constructor. It:
  - shape-validates `multi_answers` (must be list of lists)
  - cross-checks the outer length against the Request's
    sub-question count via `_expected_sub_question_count(qid)`
  - REFUSES `answer=` / `answers=` against a multi-sub-question
    Request with a clear "use multi_answers" error, so the LLM
    cannot silently drop sub-questions 2..N
  - wraps single-sub-question `answer="yes"` /
    `answers=["a", "b"]` into `[["yes"]]` / `[["a", "b"]]` for
    back-compat with single-sub-question Requests
- `__init__._build_answer_nudge_block(...)` now skips
  multi-sub-question Requests. A single-label user message
  matching an option on one sub-question of a 3-sub-question
  Request was ambiguous; the auto-nudge would have routed the LLM
  toward the broken `answer=` path. The chat LLM must compose
  `multi_answers` explicitly for multi-sub-question Requests.
- `_DISPATCHER_DIRECTIVE` gained a hard rule about
  multi-sub-question Requests: never answer with `answer=`; ask
  the user the un-addressed sub-questions instead of guessing.

### Bug #3: new questions/permissions surfaced after an agent is already AWAITING_HUMAN were never re-notified

`_phase_awaiting_human` polled `list_questions` / `list_permissions`
on every tick but consulted them ONLY for the exit gate. If a new
Q/P arrived while we were already awaiting human input on a
different signal (e.g. prose-question classifier hit, or a prior
Q/P that had not yet been resolved), the new Q/P was silently
swallowed. The operator never saw it.

Fix:

- `_phase_awaiting_human` now calls `_maybe_notify_new_pending` on
  every tick when the pending set is non-empty. The helper is
  idempotent via the `_notified_questions` / `_notified_permissions`
  per-agent id sets: ids already notified do not re-fire; newly
  observed ids do.

### Companion: SSE fast-path for question.asked / permission.asked

Detection used to wait for the next per-phase poll (~0.5–5s lag).
`_sse_consumer_loop` now subscribes to opencode's `question.asked`
and `permission.asked` bus events. Matching events trigger
`_sse_surface_pending(agent_id, session_id, worktree)` which polls
the authoritative `list_*` endpoint (we never trust the bus
payload shape directly), filters terminal-phase agents out, and
routes through the same `_maybe_notify_new_pending` helper. Lag
drops to sub-second.

### Cleanup

- `_format_permission_block` and `_build_pending_items_block` drop
  the reference to a `/oc answer <pid> <once|always|reject>` slash
  command that was never implemented. Operators are now directed
  at the opencode CLI / web UI for permission replies. A future
  `oc_permission` tool can be added on top of the already-present
  `OpencodeClient.reply_permission` if chat-side permission reply
  becomes routine.

### Documentation

[AGENTS.md](AGENTS.md) gains a new "Question / permission surface"
section pinning the Request-with-N-sub-questions shape, the
multi_answers payload contract, the AWAITING_HUMAN re-notify
behavior, and the SSE fast-path. The previous v0.20.2 sections
remain.

### Tests

+17 new tests (470 → 487 total, all passing):

- `TestFormatQuestionBlockAllInners`: single inner omits sub-index,
  multi inner renders all `#idx/total` chunks with options, empty
  inner list returns a safe stub.
- `TestBuildPendingItemsBlockMultiSubQuestion`: multi-sub-question
  renders `(N sub-questions; …)` + `#1..#N` rows + `multi_answers`
  guidance; single-sub-question Request omits the multi guidance;
  permission row no longer references the non-existent
  `/oc answer` command.
- `TestAnswerNudgeRefusesMultiSubQuestion`: label match on a
  multi-sub-question Request does NOT auto-nudge; yes/no shortcut
  is also gated to single-sub-question Requests.
- `TestMakeAnswerMultiAnswers`: multi_answers forwarded as
  `Array<Array<string>>`; outer-length mismatch is rejected
  before the wire call; `answer=` against a multi-sub-question
  Request is refused; `answer=` against a single-sub-question
  Request wraps into `[["yes"]]`; `answers=["a", "b"]` wraps into
  `[["a", "b"]]`; `multi_answers` of wrong shape (list of
  strings) is rejected.
- `TestPhaseAwaitingHumanHandler.test_new_qp_arriving_while_awaiting_fires_fresh_notification`:
  a new question id appearing in the pending list while already
  AWAITING_HUMAN fires exactly one `awaiting_human` event for the
  new id only, and a subsequent tick with the same set does not
  re-fire.
- `TestSseSurfacePending`: `question.asked` / `permission.asked`
  events trigger a notify on the matching session, terminal-phase
  agents are skipped without calling list endpoints.

## [0.20.2] - 2026-05-19

Two bugfixes that explained the BCK/p-list-tests "stuck in EXECUTING for
1h 29m, then FAILED at REVIEW_SPAWNING" incident.

### Bug A: stuck-in-EXECUTING-while-idle

The session-status SSE cache lost its event stream across an
`opencode serve` flap and stayed stuck at `{"type": "busy"}` from
before the flap. opencode does NOT re-emit `session.status: idle`
for sessions that were already idle before the SSE consumer
disconnected, so the cache never recovered. `_session_status_is_idle`
returned False on every tick, `_reset_idle_since` fired, the 120s
debounce never accumulated, the agent stayed in EXECUTING.

The polling check (`wait_idle`) was already returning True on every
tick (executor genuinely idle), but its result was consulted ONCE
and then discarded — the cache check came AFTER and overrode it.
Two independent sources of truth with no propagation between them.

Fix: unified `SessionStatusCache` class (`event_loop.py`) replaces
the free-floating `_sse_session_status` dict. Two authoritative
writers, one reader:

- **SSE consumer** -> `cache.update(agent_id, status, source="sse")`
  on every `session.status` event. Sub-second when connected.
- **HTTP poller** -> `cache.update(agent_id, {"type": "idle"}, source="poll")`
  inside the new `_wait_idle_through_cache(agent_id, session_id,
  worktree, timeout)` helper, called whenever `wait_idle` returns
  True. Closes the SSE-drop gap automatically: the next successful
  poll refreshes the cache to the authoritative truth.
- `cache.get(agent_id)` is the only reader, consulted by
  `_session_status_is_idle`. Last write wins.

All 3 executor-session `wait_idle` call sites (in `_phase_executing`
and `_phase_executor_addressing`) now route through the helper so
the cache write cannot be forgotten.

New diagnostic: `cache.get_full(agent_id)` returns
`(status, source, updated_at)` so operators can see whether the
current cached status came from SSE or polling and how stale it is.

### Bug B: stale `.review/` directory blocking REVIEW_SPAWNING

`reviewer.stage_reviewer_worktree` had `shutil.rmtree` nested inside
`except Exception` after `wt._git(check=False)`. Since `check=False`
does not raise on non-zero exit, the rmtree was dead code. If a
previous reviewer left behind gitignored content (cargo's `target/`,
`node_modules/`, `.env`, ...) when its teardown ran `git worktree
remove --force`, the directory survived as a non-empty filesystem
path. The next staging attempt failed with `git worktree add`
exit 128 because the target path was non-empty.

Fix: unconditional `git worktree remove --force` + unconditional
`shutil.rmtree(sister, ignore_errors=True)` when the path exists,
before `git worktree add`. Both calls now run on every entry to
`stage_reviewer_worktree`; the rmtree is no longer gated on an
exception that never fires.

### Internal surface

- `event_loop.SessionStatusCache` class (single source of truth)
- `event_loop._session_status_cache` (module-level instance)
- `event_loop._wait_idle_through_cache(agent_id, session_id, worktree, timeout)`
- `event_loop.get_session_status_full(agent_id)` (returns
  `(status, source, updated_at)` for diagnostics)
- `reviewer.stage_reviewer_worktree` no longer has the dead-code
  `except Exception` wrap; cleanup is unconditional

### Manual cleanup shipped alongside

Two stale `.review/` directories on the live host
(`BCK__p-list-tests.review/`, `oco__improve-dashbo-3.review/`) were
`rm -rf`'d during the v0.20.2 deploy. From v0.20.2 onward, the
defensive staging cleans these automatically.

459 -> 470 tests (+11 new for v0.20.2).

## [0.20.1] - 2026-05-19

Bugfix: archived agents no longer appear in the hourly heartbeat.

The pruner sets `archived=True` on both DONE and CANCELLED agents
12 h after the terminal transition. `heartbeat.build_report` filtered
out DONE agents past 4 h via `_visible_done`, but `CANCELLED` agents
had no analogous cutoff, so archived CANCELLED rows kept appearing in
every hourly heartbeat as noise alongside live agents.

Fix: `build_report` now skips any row with `archived=True` before
the rest of the visibility filter runs. The change also defends
against any future case where an archived DONE row slips past
`_visible_done` (e.g. if `archived` is set before `done_at`).

3 new tests in `TestHeartbeatReport`. 456 -> 459.

## [0.20.0] - 2026-05-19

Serve post-mortem capability: when `opencode serve` dies, the
orchestrator now captures the exit code, signal name, log tail, and
agents-active-at-crash into a structured `serve_crashes.jsonl` log.
This is the diagnostic layer the v0.19.x flapping investigations
demanded but didn't have. Six new surfaces.

Prior to v0.20.0 the orchestrator captured stdout+stderr per spawn
into a timestamped log file but never read the process's exit code,
never preserved the dying log across the next spawn, and surfaced
`serve_down` with no diagnostic detail beyond "5 attempts failed".

### Tier 1: exit-code + signal capture

- `transport.OpencodeClient.last_exit_info()` returns
  `{pid, exit_code, signal_name, exit_kind, uptime_sec, log_path}`.
  `exit_kind` classifies as `still_running`, `clean_exit`,
  `nonzero_exit`, `killed_by_signal`, or `unknown_already_reaped`.
- Signal-name lookup via `signal.Signals(rc)`; recognizes both
  POSIX negative-returncode (`rc = -9`) and shell-style positive
  (`rc = 137`) for SIGKILL, SIGTERM, SIGSEGV, etc.
- `OpencodeClient` now tracks `_spawn_started_at` + `_last_spawn_pid`
  so `last_exit_info()` works even after `_spawned` is cleared.
- New `last_serve_log_tail(lines=20)` returns the last N lines of
  the most recent serve log.

### Tier 2: `serve_crashes.jsonl` audit log

New file at `~/.hermes/plugins/hermes-opencode/serve_crashes.jsonl`.
Each row:

```json
{
  "ts": 1779129067.66,
  "endpoint": "0.0.0.0:4096",
  "observed_via": "ping_failed" | "restart_attempt_failed" |
                  "restart_attempt_exception" | "restart_spawn_ping_failed",
  "restart_attempt_n": 1,
  "pid": 196959,
  "exit_code": -9,
  "signal_name": "SIGKILL",
  "exit_kind": "killed_by_signal",
  "uptime_sec": 3247.2,
  "log_path": "logs/opencode-serve.20260519-001355.log",
  "log_tail": "...last 20 lines of the dying process's stdout+stderr...",
  "sec_since_last_alive": 60.3,
  "agents_active": ["BCK/p-list-tests", "BCK/prod-shortlink"]
}
```

Written by `_record_serve_crash` from:
- `_serve_watchdog_loop` when the periodic ping fails
- `_try_restart_serve_with_backoff` for every failed restart attempt
  (one row per attempt — see "spawn 1 died with SIGKILL, spawn 2 ran
  but ping returned 500, spawn 3 succeeded" instead of opaque
  "5 attempts failed")

### Tier 3: annotated `serve_down` notification

`_build_serve_down_notification` now embeds the captured exit
information into the body:

```
opencode serve at 0.0.0.0:4096 is down and 5 exponential restart
attempts failed.

Last detected exit: pid=196959 exit_kind=killed_by_signal rc=-9
  signal=SIGKILL uptime=54m
Last log lines:
  opencode server listening on http://0.0.0.0:4096
  [01:31:19.159] INFO (#283): creating instance { ... }
  fatal: heap OOM

Inspect history: `hermes oco serve-crashes` or
`~/.hermes/plugins/hermes-opencode/serve_crashes.jsonl`.
```

The notification `meta` also carries `last_crash` (full record) so
gateway adapters can render it however they like.

### Tier 4: serve-log retention

`_prune_serve_logs()` runs from the existing `_cleanup_loop` and
keeps the newest `Config.serve_log_retention_count` files (default
50). Closes the v0.19.x "60 log files and counting" growth pattern
without losing recent history needed for diagnosis.

### Tier 5: `oc_serve_crashes` tool / `hermes oco serve-crashes` CLI

- New tool: `oc_serve_crashes(limit=10)` returns recent crash
  records; LLM should call this proactively when narrating a
  `serve_down` event or answering "why did it crash?".
- New CLI: `hermes oco serve-crashes [--limit N] [--json]` —
  pretty-prints recent records with timestamp, observed_via,
  exit_kind, exit_code/signal, agents-active, and last 5 log lines.
- Tool description carries "WHEN TO USE" framing (consistent with
  v0.19.0 dispatcher directive).

### Tier 6: dashboard endpoint

- `GET /api/plugins/hermes-opencode/serve-crashes?n=20` — returns
  `{items, count, file}` from the JSONL tail. Frontend can render
  a crash-log tab without touching backend code.

### Config additions

- `Config.serve_crashes_file: Path = plugin_state_dir() / "serve_crashes.jsonl"`
- `Config.serve_log_retention_count: int = 50`

### Internal surface

- `OpencodeClient.last_exit_info() -> dict | None`
- `OpencodeClient.last_serve_log_tail(lines=20) -> str`
- `OpencodeClient._signal_name_from_returncode(rc) -> str | None`
- `event_loop._record_serve_crash(*, observed_via, restart_attempt_n,
  exit_info, log_tail_lines, extra) -> dict | None`
- `event_loop._read_serve_crashes(limit=20) -> list[dict]`
- `event_loop._prune_serve_logs() -> int` (count removed)
- `event_loop._last_serve_crash_info: dict | None` (in-memory cache
  for the next serve_down notification body)
- `event_loop._last_serve_alive_seen_at: float` (uptime since last
  observed-alive ping; included in crash record)

18 new tests; 438 -> 456 total.

## [0.19.1] - 2026-05-19

Bugfix: serve-down false escalations.

Observed during a `hermes gateway restart` on a worktree with 5
active agents: `opencode serve` flapped (down -> restart attempts ->
recovered) within a few seconds. In that window every active agent
hit 3 consecutive transport-level tick errors (ConnectError on
`wait_idle` / ReadError on `list_questions`) and the v0.18.0 3-strike
escalation correctly counted them and escalated 3 agents to FAILED.
The escalation was correct per its own contract but the underlying
cause was the supervisor's own serve-restart cycle, not real stalls.

Fix: tick failures that occur while the watchdog reports `serve_down`
(or within `SERVE_HEALING_GRACE_SEC = 30 s` after `serve_recovered`)
no longer increment `consecutive_tick_failures` and never escalate
to FAILED. They still:

- write `last_tick_error` / `last_tick_error_at` (so dashboards see
  the error)
- fire ONE `tick_error` notification per agent per unhealthy episode
  with body annotated `(opencode serve unhealthy, not counted toward
  escalation)`. Dedupe via `_unhealthy_tick_notified_agents`; the
  set entry is dropped by `_clear_tick_failure` when the agent ticks
  successfully again, so a subsequent unhealthy episode notifies
  fresh.

Healthy stalls (serve responsive but agent unresponsive) still
escalate at the 3-strike threshold exactly as in v0.18.0+.

### Internal surface

- `event_loop.SERVE_HEALING_GRACE_SEC = 30.0`
- `event_loop._serve_recovered_at: float` (stamped in `_notify_serve_recovered`)
- `event_loop._serve_is_unhealthy_or_healing() -> bool`
- `event_loop._unhealthy_tick_notified_agents: set[str]` (per-episode dedupe)
- `_clear_tick_failure(agent)` now also discards the agent_id from
  the unhealthy set unconditionally (so a successful tick resets
  the dedupe for the next episode)

10 new tests; 428 -> 438 total.

## [0.19.0] - 2026-05-19

Hermes chat UX: proactive dispatch + natural progress narration.

Previously the Hermes chat LLM saw a single static "DISPATCHER MODE"
directive and a list of pending /question entries. It could not see
which agents existed, what they were doing, what events fired between
turns, or whether the user's latest message was a task it should
dispatch. The user had to ask "what's going on?" to surface progress.

v0.19.0 rewrites the `pre_llm_call` context to be reactive to both
user intent and agent state. The system prompt stays untouched
(preserves prompt cache); all new context is appended to the user
message and is ephemeral.

### Tier 1 - Proactive dispatch

- `_DISPATCHER_DIRECTIVE` rewritten with imperative MANDATORY RULES
  framing, numbered routing rules (new task -> oc_spawn, follow-up ->
  oc_send, status -> oc_status/oc_output, stuck -> oc_retry), and an
  explicit "tool call before prose" rule.
- New `_looks_like_task()` task-verb detector + `DISPATCH NUDGE`
  block injected when the user's current message starts with an
  imperative verb (build, fix, implement, add, create, change,
  refactor, write, migrate, port, ship, ... 30+ verbs). Suppressed
  when the message ends in `?` or exceeds 4000 chars.
- Tool descriptions for `oc_spawn`, `oc_send`, `oc_status`,
  `oc_output`, `oc_answer`, `oc_retry` gained a "WHEN TO USE" lead
  sentence that maps user-message surface forms to the right tool.

### Tier 2 - Natural progress narration (proactive, no polling)

- New `Active agents` context block: every chat turn lists every
  non-terminal agent with `phase`, `session_status` (idle/busy/retry
  from the opencode SSE feed), `in_phase_for`, `pr_url`, and a 220-
  char tail snippet of the executor's latest assistant text pulled
  from the live SSE buffer. The LLM now narrates progress without
  the user asking.
- New `Since your last message` context block: per-hermes-session
  watermarks (`_session_watermarks`) track the last-seen event ts;
  every turn surfaces events that fired between turns via the new
  `event_loop.tail_recent_events(since_ts, limit)` helper that reads
  `events.log`. First-call seeds the watermark so a fresh session
  doesn't dump backlog.
- `oc_status` detailed response now includes
  `session_status` (from SSE), `last_assistant_text_snippet` (280
  chars from the SSE buffer), `last_classifier` (awaiting / source /
  reason), `phase_entered_at`, and `idle_since`.

### Tier 3 - Optional polish

- New `progress_narration` notification kind (off by default; enable
  via `plugins.entries.hermes-opencode.progress_narration.enabled`).
  `_progress_narration_loop` runs every `interval_sec` (default 5
  min), fires the event for non-terminal non-blocked agents whose
  SSE buffer changed since the last fire (dedupe via
  `_last_narrated_snippets`). In gateway mode this becomes
  unprompted DM-style progress pings.
- New `ANSWER NUDGE` block: when the user's reply token-matches an
  option label of any pending /question (or matches a yes/no token
  when exactly one question is pending), the context now tells the
  LLM "this is an answer to question_id=Q, call oc_answer" instead
  of the LLM answering them itself.

### Config additions

- `Config.progress_narration_enabled: bool = False`
- `Config.progress_narration_interval_sec: float = 300.0`
- `Config.progress_narration_snippet_chars: int = 280`
- `Config.notify_events` default now includes `progress_narration`,
  `needs_intervention`, `phase_stuck` (the v0.18.0 kinds were already
  in the dataclass default but the `from_plugin_entry` default-set
  was stale).

### Public surface

- `event_loop.tail_recent_events(since_ts: float, limit: int = 50)`
- `event_loop._build_narration_snippet(agent_id, max_chars)`
- `_build_active_agents_block`, `_build_recent_events_block`,
  `_build_dispatch_nudge_block`, `_build_answer_nudge_block`,
  `_looks_like_task`, `_session_watermarks` (in `__init__.py`)
- `_pre_llm_call_hook` signature now accepts `session_id` and
  `user_message` kwargs (back-compat: defaults preserve old
  behaviour).

26 new tests; 402 -> 428 total.

## [0.18.0] - 2026-05-19

Five-tier resilience overhaul. Six of the nine hard `FAILED`
transitions in the state machine now have a phase-aware retry path,
the most operator-recoverable failures route to a new
`NEEDS_INTERVENTION` phase instead of `FAILED`, and a single
`oc_retry` tool resurrects FAILED / NEEDS_INTERVENTION agents and
kicks stuck non-terminal agents.

### Tier 1 — Per-phase retry budgets

New `Agent.phase_retry_count` + per-phase ceilings. Replaces "first
error -> FAILED" with a phase-scoped retry budget. `AgentStore.update`
auto-resets the counter on every phase change (and auto-stamps
`phase_entered_at`) so the budget is naturally scoped per-phase
without per-site bookkeeping. Sites converted:

- QUEUED first-prompt send (ceiling 5)
- REVIEW_SPAWNING staging + session create (ceiling 5)
- REVIEW_DELIVERED addressing-prompt dispatch (ceiling 5)
- REVIEWING "reviewer state lost" recovers by re-staging rather
  than failing outright

`PHASE_RETRY_CEILING` is a per-phase dict; default is 3.

### Tier 2 — Differentiated abort nudges

`_check_executor_abort` now varies the nudge by attempt:

1. attempt 1: `continue`
2. attempt 2: `You stopped mid-task. Resume where you left off ...`
3. attempt 3: `[SYSTEM DIRECTIVE: HERMES-OPENCODE - RESUME] ...`

Same 3-strike escalation; each attempt has a different chance of
landing instead of three identical `continue` pokes.

### Tier 3 — `NEEDS_INTERVENTION` phase

New non-terminal phase, sibling of `AWAITING_HUMAN`:

- `AWAITING_HUMAN` = executor itself paused for user reply
- `NEEDS_INTERVENTION` = orchestrator couldn't proceed, needs
  operator decision

Routes:

- PR-fallback exhausted in `_phase_committing` -> `NEEDS_INTERVENTION`
  instead of `FAILED`. Operator fixes `gh auth` / network, then runs
  `oc_retry`.
- Future failure sites that have a clear operator action can opt into
  this with `_handle_phase_failure(..., on_exhausted_intervene=True)`.

Fires the new `needs_intervention` notification (default-on).

### Tier 4 — `oc_retry` tool (NEW)

Three modes, all routed through one tool:

- `FAILED` agent: restore `phase_before_failed`, clear retry counts
  + tick-failure streak.
- `NEEDS_INTERVENTION` agent: restore `phase_before_intervention`,
  clear the intervention reason.
- Any other non-terminal agent: reset `phase_retry_count` and
  `last_tick_error`, force an immediate re-tick. Useful after a
  gateway restart, transient network outage, or to kick a stuck
  agent.

Refuses on `DONE` / `KILLED` / `CANCELLED`, and on `FAILED` agents
whose `last_error` says `project gone` (truly unrecoverable).

Available via:

- Tool: `oc_retry(agent_id)`
- Slash command: `/oc retry <agent_id>` (works in CLI + gateway DM)
- CLI: `hermes oco retry <agent_id>`

### Tier 5 — Phase-stuck watchdog

New `_phase_stuck_loop` runs every 60 s. Flags any agent stuck in a
transitional / short-lived phase for > `STUCK_WARN_SEC` (10 min).
Watched phases:

`CREATED, BOOTSTRAPPING, IDLE_TASK_COMPLETE, REVIEW_SPAWNING,
REVIEW_DELIVERED, IDLE_REVIEW_ADDRESSED, COMMITTING`

Skips intentionally-long phases (EXECUTING, REVIEWING, PR_OPEN), and
blocked phases (AWAITING_HUMAN, NEEDS_INTERVENTION, RATE_LIMITED,
QUEUED). Fires `phase_stuck` notification once per stuck-period;
re-arms when the phase changes.

### State / API additions

- `Agent.phase_retry_count: int = 0`
- `Agent.phase_entered_at: float` (auto-stamped on phase change)
- `Agent.phase_before_failed: str | None` (stamped at FAILED transitions)
- `Agent.phase_before_intervention: str | None` (stamped at NEEDS_INTERVENTION entry)
- `Agent.intervention_reason: str | None`
- `Agent.intervention_since: float | None`
- `Agent.last_stuck_notify_at: float | None`
- `state.TERMINAL_PHASES` (canonical frozenset; event_loop.py now imports it)
- `event_loop.PHASE_RETRY_CEILING` + `PHASE_RETRY_CEILING_DEFAULT`
- `event_loop.STUCK_WARN_SEC = 600.0`, `STUCK_CHECK_INTERVAL_SEC = 60.0`, `STUCK_WATCHED_PHASES`
- `event_loop.ABORT_NUDGE_PROMPTS` (3 escalating prompts)
- `tools.RETRY_SCHEMA` + `make_retry`
- 2 new notification kinds (`needs_intervention`, `phase_stuck`)
  added to `Config.notify_events` default set.

## [0.17.0] - 2026-05-18

Hardens the awaiting-input / review-readiness gate against three
classes of false positives, and introduces an authoritative
executor-emitted sentinel for review readiness.

### Awaiting-input cascade fixes

- **Reasoning leak**: opencode emits `message.part.delta` events with
  `field="text"` for BOTH text and reasoning parts. The SSE consumer
  previously buffered reasoning deltas alongside actual assistant
  text. Now it tracks part types from prior `message.part.updated`
  events and skips reasoning. The awaiting-input classifier (and any
  consumer of `_fetch_last_assistant_text`) no longer sees inner
  monologue.
- **User-message leak**: the SSE consumer also buffered text parts
  from user messages. Now it tracks message roles from
  `message.updated` events and only buffers parts whose parent
  message has `role="assistant"`. The classifier no longer mistakes
  the user's prompt for an assistant question.
- **Legacy `_last_assistant_text` reader**: the items-list reader
  used for reviewer text now filters by assistant role too.

### Todo-list signal

- New `awaiting_input.check(..., has_incomplete_todos=)` parameter +
  `todo-override` source. When the executor's opencode todo list has
  any non-completed item (or a todowrite call still in flight), the
  cascade short-circuits with `awaiting=False`, preventing the
  classifier from interpreting in-progress narration as a question.
- `event_loop._has_incomplete_todos(items)` parses the latest
  `todowrite` tool part to determine open-todo state.
- `_awaiting_input_blocks_review` and `_phase_awaiting_human` both
  feed the todo state into the cascade.

### Authoritative review-readiness signal

- New `READY_FOR_REVIEW` sentinel. The executor is now instructed via
  `ORCHESTRATOR_DIRECTIVE` (rule 2) to emit `READY_FOR_REVIEW` on its
  own line when the task is complete. `reviewer.parse_ready_for_review`
  detects it.
- When the sentinel is present (and there is a non-empty diff), the
  orchestrator transitions immediately to `IDLE_TASK_COMPLETE` /
  `COMMITTING` without waiting for the 2-minute idle debounce.

### Session-status SSE tracking

- The SSE consumer now subscribes to `session.status` events and
  caches the latest `{type: "idle"|"busy"|"retry"}` payload per
  agent. `get_session_status(agent_id)` exposes it.
- `_phase_executing` / `_phase_executor_addressing` consult this
  authoritative server-side signal before any heuristic. While
  status is `busy` or `retry`, the idle-since timestamp is reset
  and the agent stays in EXECUTING.

### Idle debounce

- `IDLE_DEBOUNCE_SEC` raised from `30.0` to `120.0`. Premature review
  was the primary failure mode of v0.16.x. Two minutes of confirmed
  idleness is the new floor.
- The debounce is now a wall-clock timestamp check on the new
  `Agent.idle_since` field, not a blocking `asyncio.sleep`. The tick
  continues to run every cycle so rate-limit, abort, and
  question/permission arrivals are observed promptly.

### State / API additions

- `Agent.idle_since: float | None` — when the executor session first
  went idle. Cleared on any sign of activity.
- `Agent.ready_for_review_at: float | None` — when the sentinel was
  observed (audit only).

## [0.16.4] - 2026-05-18

Drops the `opencode_server.url` knob entirely. Server config is now
`host` + `port` only. The connect URL is constructed internally as
`http://{host}:{port}`; opencode's serve CLI does not accept `--url`,
so keeping a derived knob caused parsing drift between YAML, the
spawn cmdline, and status output. Status/log messages now print
`{host}:{port}` instead of a URL.

### Changed (BREAKING)

- **`Config` shape: `host` + `port` only.** The `server_url` field +
  `DEFAULT_SERVER_URL` constant are removed. Two new fields:
  `host: str = "127.0.0.1"` and `port: int = 4096`. New properties
  `Config.endpoint` (`f"{host}:{port}"`, for user-facing display) and
  `Config.connect_url` (`f"http://{host}:{port}"`, for internal client
  construction).

- **`OpencodeClient` signature: `(host, port, password=None)`.** Was
  `(base_url, password=None, host=None)` which parsed `base_url`
  internally and split bind vs connect host. The new constructor
  takes host + port directly; `base_url` is built once as
  `http://{host}:{port}`. The previous `_connect_host` / `_bind_host`
  attributes are gone (no split — `--hostname={host} --port={port}`
  goes to `opencode serve`, and httpx connects to the same
  `{host}:{port}`).

- **YAML schema:**

  ```yaml
  # OLD (v0.16.3 and earlier):
  opencode_server:
    url: "http://127.0.0.1:4096"
    host: "0.0.0.0"  # bind override

  # NEW (v0.16.4):
  opencode_server:
    host: "127.0.0.1"
    port: 4096
  ```

  Any `url` setting in user YAML is silently ignored. `host` and
  `port` are read from `opencode_server.host` / `opencode_server.port`
  with env-var fallbacks `OPENCODE_HOST` and `OPENCODE_PORT`.
  Defaults: `127.0.0.1` and `4096`.

- **Status / log output uses `host:port`, not URL.** `/oc doctor`
  prints `opencode server · 127.0.0.1:4096`. The serve-down /
  serve-recovered notification bodies say `\`opencode serve\` at
  127.0.0.1:4096 is down`. Event metadata key renamed `server_url`
  -> `endpoint`.

- Dashboard JSON field `server_url` in API responses is preserved
  (frontend compat) but now sourced from `Config.connect_url`.

### Why

The user explicitly requested this in the v0.16.0 work: "pass only
`--host` and `--port`, not `--url`, as they can conflict." v0.16.0
addressed the spawn-cmdline part (and v0.16.2 fixed the wrong
`--host=` -> `--hostname=` revert) but kept the misleading `url`
config knob and `server_url` plumbing throughout. v0.16.4 finishes
the cleanup so there is exactly one source of truth for where
opencode serve binds and where the plugin connects: `host` + `port`.

### Migration

Users with `opencode_server.url: "http://x:y"` in their YAML must
change to:

```yaml
opencode_server:
  host: "x"
  port: y
```

Users with no opencode_server config get the new defaults
(`127.0.0.1:4096`) which match the previous default URL exactly.

### Tests

`TestHostPortConfig` (6 tests) replaces `TestHostConfig`. Covers
defaults, YAML reads, env-var fallback (`OPENCODE_HOST` +
`OPENCODE_PORT`), YAML-over-env precedence, client construction,
and the no-loopback-substitution invariant. Full suite: 357 passed
(was 358 at v0.16.3 — net -1 from consolidating the previous 8
bind/connect tests down to 6 unified ones).

## [0.16.3] - 2026-05-18

Day-one bugfix surfaced by v0.16.2: `load_entry_config()` has been
silently returning `{}` since the plugin was first written, so every
user-set value in `plugins.entries.hermes-opencode.*` was ignored
and only the `Config` dataclass defaults ever applied. The user's
`opencode_server.host: 0.0.0.0` setting from the v0.16.2 host-rename
work would have had no effect on the actual spawn flag even after
the CLI flag was correct, because `host` was never read.

### Fixed

- **`load_entry_config()` now correctly reads the plugin's YAML
  entry.** The original implementation called
  `cfg_get(f"plugins.entries.{PLUGIN_NAME}", {})` against
  `hermes_cli.config.cfg_get`, whose signature is
  `(cfg_dict, *positional_path_keys, default=...)`. Passing a
  dotted-string path made `cfg_get` see a non-dict first arg and
  return the default (`None`); the `or {}` then converted that to
  `{}`, so `Config.from_plugin_entry({})` ran with no user input and
  returned an all-defaults Config object.

  v0.16.3 walks the path correctly:

  ```python
  cfg = load_config()
  entry = cfg_get(cfg, "plugins", "entries", PLUGIN_NAME, default={})
  ```

  Plus a raw `yaml.safe_load` fallback for environments where
  `hermes_cli.config` is not importable. Both paths are pinned by
  `TestLoadEntryConfigReadsUserYaml` (4 tests).

  Settings that have ALWAYS been silently ignored and now finally
  take effect:

  - `opencode_server.url` / `opencode_server.host` /
    `opencode_server.password` / `opencode_server.pr_fallback_models`
  - `pr.base_branch`
  - `auto_spawn_server`
  - `review.max_cycles` (and every other review.* knob)
  - `notify.sinks` / `notify.gateway.platform` / `notify.gateway.chat_id`
    / `notify.events.enabled`
  - `heartbeat.enabled` / `heartbeat.timezone` /
    `heartbeat.unconditional_hours`
  - `classifier.enabled` / `classifier.task` /
    `classifier.max_input_chars` / `classifier.max_output_tokens` /
    `classifier.timeout_sec`
  - `awaiting_input.stall_timeout_sec` /
    `awaiting_input.reminder_interval_sec`

  Users who had any of these set in their YAML have been running on
  the dataclass defaults since v0.3.0. After v0.16.3 the user's
  config takes effect for the first time. Behaviour may change for
  users who set any of these values; the change is correctness, not
  regression. Review your `~/.hermes/config.yaml` plugin entry
  before upgrading if you've relied on the previous (broken)
  silent-default behaviour.

### Added

- 4 new tests in `TestLoadEntryConfigReadsUserYaml`:
  - hermes_cli path passes positional keys (not dotted string).
  - raw-YAML fallback walks the path correctly.
  - raw-YAML fallback returns `{}` when the plugin entry is absent.
  - raw-YAML fallback returns `{}` when the config file is absent.

  Full suite: 358 passed (was 354 at v0.16.2).

## [0.16.2] - 2026-05-18

Two bugfixes shipped together: (1) opencode CLI spawn flag regression
that broke `opencode serve` startup, and (2) `awaiting_human_resumed`
firing spuriously on classifier non-determinism (gateway restart
notification "Human reply received" when no human had replied), plus
restored full last-assistant-text in awaiting-human notifications
(previously head-truncated with ellipsis).

### Fixed

- **`opencode serve` spawn flag reverted to `--hostname=`.** v0.16.0
  renamed the YAML knob `serve_hostname` -> `host` AND the outgoing
  CLI flag `--hostname=` -> `--host=` in one step. The YAML rename
  was intentional and user-facing; the CLI flag rename was wrong
  because opencode's actual `serve` subcommand accepts `--hostname`
  (visible in `opencode serve --help`), not `--host`. The bad flag
  caused every spawn to fail with `opencode serve exited during
  startup (rc=1)` and the executor never started. v0.16.2 emits
  `--hostname=` again. YAML / dataclass / kwarg name stays `host` as
  user requested in v0.16.0; only the wire-level CLI flag is
  reverted. New `TestServeCmdlineUsesOpencodeHostnameFlag` patches
  `subprocess.Popen` to pin the cmdline so the regression cannot
  recur.

- **`awaiting_human_resumed` no longer fires on classifier flip.**
  v0.16.0's `_phase_awaiting_human` exited AWAITING_HUMAN whenever
  pending `/question` + `/permission` were empty AND the
  awaiting-input classifier said "not awaiting" on the latest
  assistant text. The classifier is an LLM heuristic and flips
  non-deterministically on borderline prose (e.g. `Before I pick a
  direction, let me confirm scope.`). On gateway / process restart
  the supervisor re-ran `_phase_awaiting_human` first tick, the
  classifier disagreed with itself vs entry, the agent's phase was
  restored, and a misleading `Human reply received after Xm` event
  fired with no human input.

  v0.16.2 makes classifier verdict alone insufficient. Exit now
  requires authoritative forward-progress signal:

  1. Entry was triggered by a pending `/question` / `/permission`
     (`awaiting_entry_had_pending_qp=True`) and the pending set is
     now empty. The opencode server is the source of truth on
     question / permission resolution.
  2. Entry was triggered by prose-question classifier alone
     (`awaiting_entry_had_pending_qp=False`) AND the latest assistant
     `message.id` differs from `awaiting_entry_message_id`. A new
     assistant turn proves the executor moved past the awaiting
     state (the only way it produces a new turn while paused is for
     a human to type a reply via opencode CLI / web UI). In this
     path the classifier is re-run on the NEW text; if it flags the
     new turn as also asking, the entry message-id is re-anchored
     to the new turn and the agent stays AWAITING_HUMAN.

  New `Agent` fields `awaiting_entry_message_id: str | None` and
  `awaiting_entry_had_pending_qp: bool` capture entry context.
  `_enter_awaiting_human` is now `async` so it can fetch the latest
  message id at entry; on idempotent re-entry the first trigger
  wins. `_maybe_notify_new_pending` and `_maybe_notify_awaiting_classified`
  also became `async` to support this.

  Legacy agents in AWAITING_HUMAN before this release have
  `awaiting_entry_message_id=None`. First tick after upgrade
  backfills the field from the current latest assistant message id
  and sleeps; the next tick runs the new exit gate.

  Event body for the poll-driven exit no longer claims "Human reply
  received" (which was a lie). It now reports the actual signal:
  "Pending question/permission resolved" or "Executor produced new
  assistant turn". The v0.16.1 helper (`_resume_from_awaiting_human`)
  still uses the precise `reason` it was given by its caller (e.g.
  "oc_send human reply").

- **Full last-assistant-text in awaiting-human notifications.**
  Three sites in `event_loop.py` head-truncated the context that
  surfaced in the gateway DM / dashboard notification body:
  `_maybe_notify_new_pending` (500 chars), `_maybe_notify_awaiting_classified`
  (800 chars), and `_run_awaiting_input_reminders` (600 chars).
  Truncated output was prefixed with `... ` which made the message
  start in the middle of a sentence and obscured the context the
  human needed to answer the executor's question. All three sites
  now emit the full text. Three new tests in
  `TestAwaitingContextNotTruncated` pin this with multi-KB samples.

### Added

- 11 new regression tests across `TestServeCmdlineUsesOpencodeHostnameFlag`,
  `TestAwaitingContextNotTruncated`, `TestPhaseAwaitingHumanHandler`
  (new-turn / classifier-flip / legacy-backfill / multi-turn re-anchor
  scenarios). Full suite: 354 passed (was 346 at v0.16.1).

## [0.16.1] - 2026-05-17

Closes a v0.16.0 UX gap: a human reply on an `AWAITING_HUMAN` agent
now transitions the agent immediately, instead of waiting for the
next `_phase_awaiting_human` poll tick.

### Fixed

- **Immediate resume on human input.** v0.16.0 introduced
  `AWAITING_HUMAN` as a proper phase, with exit detected by
  `_phase_awaiting_human` polling `list_questions` / `list_permissions`
  and re-running the classifier. The exit could take a tick (a few
  seconds) after the human replied, leaving the dashboard showing
  `AWAITING_HUMAN` longer than necessary and giving the impression
  the reply didn't land.

  v0.16.1 wires a new `event_loop._resume_from_awaiting_human(agent,
  reason)` helper into every human-input dispatch surface:

  - `oc_answer` tool (after a successful `/question` reply or reject).
  - `oc_send` tool (after `send_message_async` succeeds).
  - `@<agent_id> <text>` direct gateway dispatch (after
    `send_message_async` succeeds inside `_handle_at_agent_dispatch`).

  On dispatch success, if the agent is currently in `AWAITING_HUMAN`,
  the helper restores `phase_before_awaiting` (default `EXECUTING`),
  clears the awaiting bookkeeping fields, and fires the existing
  `awaiting_human_resumed` event. No-op when the agent is not in
  `AWAITING_HUMAN` so the surfaces work the same for non-awaiting
  agents.

  `_phase_awaiting_human`'s poll-based exit path remains as a safety
  net for the case where the human resolves the question by some
  other means (e.g. opencode-side `/question` answered via a separate
  client). Both paths converge on the same state-update +
  `awaiting_human_resumed` notification.

### Added

- **5 new regression tests** in `tests/test_pure_logic.py::TestResumeFromAwaitingHuman`:
  - helper restores `phase_before_awaiting` (incl. `EXECUTOR_ADDRESSING`).
  - helper defaults to `EXECUTING` when no prior phase saved.
  - helper is a noop when agent is not in `AWAITING_HUMAN`.
  - `oc_send` to an `AWAITING_HUMAN` agent transitions it back without
    a poll tick AND fires `awaiting_human_resumed`.
  - `oc_send` to a non-`AWAITING_HUMAN` agent does NOT spuriously fire
    `awaiting_human_resumed` (the no-op guard works).

  Full suite: 346 passed (was 341 at v0.16.0).

## [0.16.0] - 2026-05-17

Minor bump: introduces a new `AWAITING_HUMAN` phase (previously only an
event kind), the `/oc spawn` + `/oc resume-pr` slash commands, the
matching `hermes oco spawn` + `hermes oco resume-pr` CLI subcommands,
and a new `oc_resume_pr` tool for continuing work on an existing open PR.
Also renames the v0.14.5 bind-host knob `serve_hostname` -> `host` and
the spawn flag `--hostname=` -> `--host=`.

### Breaking config rename

- **`opencode_server.serve_hostname` -> `opencode_server.host`** (YAML).
- **`OPENCODE_SERVE_HOSTNAME` -> `OPENCODE_HOST`** (env var).
- **`Config.serve_hostname` -> `Config.host`** (dataclass field).
- **`OpencodeClient(host=...)`** kwarg (was `serve_hostname=`).
- **`opencode serve --host=<value>`** spawn flag (was `--hostname=`).
- Internal `_serve_hostname` -> `_bind_host`; `_host` (the connect
  loopback host) -> `_connect_host` for clarity in transport.py.

Users of the v0.14.5 / v0.15.x knob must rename their YAML key on
upgrade; the old key is no longer read. There was no other tool /
slash / CLI surface that referenced the old name, so the migration is
purely a hermes config edit.

### Added

- **`AWAITING_HUMAN` is now a proper phase**, surfaced on the dashboard
  with the `✋` glyph and an amber color so a viewer can see at a glance
  that an agent is paused on human input — without relying on the
  `awaiting_human` DM notification stream.

  New `_enter_awaiting_human(agent, body)` helper in event_loop.py
  transitions the agent to `phase=AWAITING_HUMAN`, saves
  `phase_before_awaiting` + `awaiting_human_since`, and fires the
  existing `awaiting_human` notification. Idempotent on re-entry
  (reminder loop fires don't reset `awaiting_human_since` or overwrite
  `phase_before_awaiting`).

  All three call sites that fired the awaiting-human event now route
  through the helper:
  - `_maybe_notify_new_pending` (executor emitted `/question` or
    `/permission` opencode API entries).
  - `_maybe_notify_awaiting_classified` (executor wrote prose that the
    classifier flagged as awaiting; called from
    `_awaiting_input_blocks_review`).

  New `_phase_awaiting_human(agent)` handler polls each tick:
  - If pending `/question` or `/permission` still exists for the
    executor session, stay in AWAITING_HUMAN and return.
  - Otherwise re-run the awaiting-input classifier on the latest
    assistant text. If still awaiting, stay.
  - If both detectors say not-awaiting, restore
    `phase_before_awaiting` (default EXECUTING) and fire the new
    `awaiting_human_resumed` event so the dashboard and DM watchers
    see forward progress.

  `_run_awaiting_input_reminders` now scans `phase == "AWAITING_HUMAN"`
  exclusively. The v0.14.x scan of EXECUTING / EXECUTOR_ADDRESSING is
  retired because every awaiting agent is now in AWAITING_HUMAN.

  Dashboard + CLI phase-glyph tables (`commands.py::_PHASE_GLYPH`,
  `dashboard/src/index.jsx::PHASE_GLYPH`) gain `AWAITING_HUMAN: "✋"`.
  Dashboard CSS gains an amber-bold rule for the new phase plus
  muted/orange rules for the v0.15.x `QUEUED` and `RATE_LIMITED`
  phases that were missing previously.

  New `Agent` fields: `phase_before_awaiting: str | None`,
  `awaiting_human_since: float | None`.

  New `awaiting_human_resumed` event kind in the default
  `notify_events` set.

- **`/oc spawn <project> <task> <prompt>` slash command + `hermes oco
  spawn <project> <task> <prompt> [--branch …] [--base-branch …]
  [--agent …]` CLI subcommand**. Both surfaces route through the
  existing `make_spawn` handler (via `event_loop.run_blocking` for the
  slash path, direct `asyncio.run` for the standalone CLI). Removes
  the prior limitation that `oc_spawn` was only callable via the
  chat-LLM tool surface — useful for scripting / cron, gateway DM
  triggers, and bypassing the chat LLM entirely when the prompt is
  pre-decided.

  The CLI prints a hint when it doesn't detect a running hermes
  process (the agent record is persisted, but the bg event loop only
  ticks while hermes is running; the agent will resume normally on
  next hermes start).

- **`/oc resume-pr <project> <pr_num> [--skip-review] <prompt>` slash
  command + `hermes oco resume-pr <project> <pr_num> <prompt>
  [--skip-review]` CLI subcommand + `oc_resume_pr` tool**. Resumes
  work on an existing OPEN pull request. Flow:

  1. `gh pr view <num> --json headRefName,state,url,number` (run with
     `cwd=project.repo_path`). Reject if state != OPEN.
  2. `git fetch origin <branch>` to make sure the local repo has the
     PR's branch ref.
  3. `wt.compose_agent_id(project.abbrev, f"resume-pr-{pr_num}", …)`
     to derive a unique agent_id.
  4. `wt.create_worktree(repo, target, branch, base)` — the existing
     helper already handles "branch exists -> check out via
     `worktree add <target> <branch>` (no `-b`)", so the new worktree
     lands on the PR's branch directly.
  5. Bootstrap normally, create opencode session, persist Agent
     record with `pr_url` + `pr_number` pre-populated.
  6. Send the wrapped prompt via `send_message_async`.

  When `skip_review=true`, the Agent record is created with
  `review_cycle_count = config.review_max_cycles` so the review cycle
  is treated as already-exhausted; the agent jumps from EXECUTING to
  COMMITTING the moment it's idle with a diff. Use for trivial
  follow-ups (typo fixes, comment edits) where re-running the
  reviewer is wasteful.

  Same dispatcher discipline applies: `prompt` is forwarded VERBATIM.

  Spawn-gate honored: if any agent is in `RATE_LIMITED`, the
  resume-pr agent enters `phase=QUEUED` with `queued_blocked_by`
  populated, exactly like `oc_spawn`.

- **18 new regression tests** in `tests/test_pure_logic.py`:
  - `TestAwaitingHumanPhase` (3): enter saves prior phase; from
    EXECUTOR_ADDRESSING vs EXECUTING; idempotent re-entry.
  - `TestPhaseAwaitingHumanHandler` (5): pending question keeps phase;
    pending permission keeps phase; classifier still-awaiting keeps
    phase; clear restores prior + fires resumed; clear with no prior
    defaults to EXECUTING.
  - `TestAwaitingHumanReminderLoopScansNewPhase` (2): AWAITING_HUMAN
    fires reminder; EXECUTING no longer triggers reminder.
  - `TestOcSpawnSlashCommand` (3): help on no args; --help/help flags;
    too few args.
  - `TestOcResumePrSlashCommand` (3): help; invalid pr_number;
    --skip-review parsed regardless of position.
  - `TestResumePrHandler` (2): unknown project; PR not OPEN.

  Full suite: 341 passed (was 323 at v0.15.2).

### Changed

- `_PHASE_HANDLERS` gained `AWAITING_HUMAN -> _phase_awaiting_human`.
- `_OC_HELP_TEXT` (slash help) gained `/oc spawn`, `/oc resume-pr`,
  and a direct-dispatch section pointing at `@<agent_id> <text>`
  (v0.14.4).
- `hermes oco --help` lists `spawn` + `resume-pr` in the usage line.
- `plugin.yaml::provides_tools` gained `oc_resume_pr`.
- `dashboard/dist/index.js` rebuilt via `bun run build`.
- `AGENTS.md` state-machine section gained a new paragraph documenting
  AWAITING_HUMAN entry/exit semantics.

### Internal notes

The v0.15.0 design choice for the rate-limit recovery (review NOT
bypassed; wait-and-resume) applies the same way here: the awaiting-
human recovery path simply restores the saved prior phase and lets
the agent continue through its own normal flow. No
shortcuts, no auto-`continue` (that's the abort path, not the
awaiting-human path).

## [0.15.2] - 2026-05-17

### Fixed

- **`/oc attach` and `/oc cancel` no longer crash with
  `RuntimeError: asyncio.run() cannot be called from a running event loop`.**
  Both slash-command handlers ([`make_oc_attach`](commands.py) and
  [`make_oc_cancel`](commands.py)) were sync functions that called
  `asyncio.run(...)` to dispatch the opencode HTTP round-trip. From the
  hermes TUI dispatch path and the `pre_gateway_dispatch` hook path,
  these handlers run on threads where hermes' main asyncio loop is
  already active, so `asyncio.run` raised immediately and the user saw
  the bare exception instead of the transcript / cancel result. The
  same bug class affected the `notify_gateway` sink's adapter `send`
  call when `model_tools._run_async` is unavailable
  ([`notify._send_gateway`](notify.py)), where it manifested as a
  silently failed `NotifyResult("gateway", False, "asyncio.run failed: ...")`
  rather than a crash.

### Added

- **`event_loop.run_blocking(coro_factory, *, timeout=60.0)`** —
  canonical helper for sync slash-command and hook handlers that need
  to await an opencode HTTP call. Routes coroutines through the
  plugin's already-running background event loop via
  `asyncio.run_coroutine_threadsafe`, so it works whether or not the
  caller's thread has its own running loop. Falls back to
  `asyncio.run` only when the bg loop isn't running AND the caller
  has no running loop (the standalone `hermes oco …` CLI path).
  Raises a descriptive `RuntimeError` when inside a running loop with
  no bg loop available (a plugin-registration error that won't occur
  at runtime). Matches the existing `schedule()` signature — takes a
  zero-arg factory so coroutines are never created in contexts that
  won't consume them. All three previously broken call sites
  (`make_oc_attach`, `make_oc_cancel`, `_send_gateway`) now route
  through this helper.

  Originally proposed as v0.14.6 in PR #7 (branch `oco/fix-attach`);
  rebased onto current `main` and renumbered to v0.15.2 because the
  v0.14.6 / v0.15.0 / v0.15.1 slots were claimed in-flight by other
  releases.

## [0.15.1] - 2026-05-17

Closes the known gap documented in v0.15.0: reviewer-session
rate-limit detection.

### Added

- **Generic session-level rate-limit detector**
  `_check_session_rate_limited(agent, session_id, worktree, session_label)`
  in event_loop.py. Same semantics as v0.15.0's
  `_check_executor_rate_limited` (transition agent to RATE_LIMITED,
  save `phase_before_rate_limit`, record retry-after window, fire
  `rate_limited` notification) but parameterized by session_id +
  worktree + label so it can run against the executor OR the reviewer
  session.

- **`_phase_reviewing` now detects reviewer-session rate limits.**
  After `wait_idle` succeeds, the handler calls
  `_check_session_rate_limited(agent, agent.reviewer_session_id,
  sister, session_label="reviewer")`. On a 429 hit during review, the
  agent transitions to RATE_LIMITED with
  `phase_before_rate_limit="REVIEWING"`, so `_phase_rate_limited`'s
  wait-and-resume path restores it back to REVIEWING when the limit
  clears. Same behavior as the executor-side path: review is NOT
  bypassed.

- **Notification body + `last_error` now include the session label.**
  Previously the rate-limit notification was generic ("rate-limited by
  provider; retry in Ns"). Now it reads "rate-limited by provider on
  reviewer session (phase=REVIEWING); ..." so the user sees which
  session hit the limit. `agent.last_error` carries the same label.

- **5 new regression tests** in `tests/test_pure_logic.py::TestCheckSessionRateLimited`:
  - reviewer-session 429 transitions agent with reviewer label in body
    and `last_error`
  - executor-session 429 emits "executor session" label (precedence /
    string-content check)
  - no rate-limit returns False without notify
  - back-compat: `_check_executor_rate_limited` still routes through
    the generalized helper
  - already-RATE_LIMITED is a noop on either session

  Full suite: 319 passed (was 314 at v0.15.0).

### Changed

- `_check_executor_rate_limited(agent)` is now a thin back-compat
  wrapper around `_check_session_rate_limited(agent, agent.session_id,
  Path(agent.worktree_path), session_label="executor")`. All v0.15.0
  callsites (`_phase_executing`, `_phase_executor_addressing`, two
  call points inside `_phase_committing`) continue to work unchanged.

### Docs

- `AGENTS.md::Rate limits and queue` section gained a note on the
  v0.15.1 generalization and the new `session_label` convention for
  future handlers that touch additional sessions.

## [0.15.0] - 2026-05-17

Minor bump: introduces two new phases (`QUEUED`, `RATE_LIMITED`),
spawn-time gating semantics, and a configurable non-Anthropic model
fallback list for PR creation. Replaces the v0.14.6 slug+initial_prompt
PR-fallback with iterative model-fallback `oneshot_open_pr`.

### Added

- **Provider rate-limit detection + wait-and-resume.** When the
  executor's upstream provider rate-limits (most often Anthropic Claude
  HTTP 429), opencode marks the assistant turn with
  `message.error = { name: "APIError", statusCode: 429, ... }` (per
  opencode/packages/opencode/src/session/message-v2.ts).

  New `_message_is_rate_limited(item)` extracts the retry-after window
  from `responseHeaders.retry-after`, `responseHeaders.x-ratelimit-reset-after`,
  or `metadata.retryAfterMs` — falling back to a `RATE_LIMIT_MIN_WAIT_SEC =
  30s` floor when none are present. Also accepts textual markers
  ("rate limit", "quota exceeded", "too many requests", "429") on the
  error message body as a defensive fallback.

  New `_check_executor_rate_limited(agent)` runs at the top of
  `_phase_executing`, `_phase_executor_addressing`, and twice in
  `_phase_committing` (before and after `executor_open_pr`). On a hit,
  it transitions the agent to `phase=RATE_LIMITED`, saves the prior
  phase to `phase_before_rate_limit`, records the retry-at timestamp,
  and fires the new `rate_limited` notification.

  New `_phase_rate_limited(agent)` handler polls until
  `rate_limit_retry_after_at` elapses, then restores the saved phase,
  clears the rate-limit fields, fires `rate_limit_cleared`. The agent
  continues through its own normal flow (review, executor-addressing,
  committing) — review is NOT bypassed. This matches the v0.15.0 design
  choice: wait for the limit to reset and let the task go through its
  own flow.

- **Concurrent spawn gating via `QUEUED` phase.** While any non-terminal
  agent is in `RATE_LIMITED`, `oc_spawn` parks new agents at
  `phase=QUEUED` with `queued_blocked_by=[<ids>]`. The worktree, session,
  and bootstrap run normally — only the initial prompt is deferred. New
  `_phase_queued(agent)` handler polls (every `QUEUE_POLL_SEC = 5s`)
  until no `RATE_LIMITED` agents remain, then sends the wrapped initial
  prompt and transitions to `EXECUTING`, firing `queue_drained`. The
  blocked_by list self-heals across ticks when a different agent
  rate-limits.

  `oc_spawn` result shape gains `blocked_by: list[str]` when queued.
  Existing `queued: true` return field is retained (it was already used
  for "first turn queued asynchronously").

- **Configurable non-Anthropic PR-fallback model list.** New
  `Config.pr_fallback_models: list[str]` (default
  `["openai/gpt-5.5", "opencode/deepseek-v4-flash-free"]`). Read from
  YAML `plugins.entries.hermes-opencode.opencode_server.pr_fallback_models`
  (preferred) or env `OPENCODE_PR_FALLBACK_MODELS` (comma-separated
  fallback). YAML list wins; empty/missing falls through to env then
  to the default.

  New `reviewer.parse_model_id(spec)` converts the `provider/model[/variant]`
  spec into opencode's POST `/session` model struct
  `{ "id": ..., "providerID": ..., "variant"?: ... }`. Returns `None`
  for malformed input (no slash, only one segment, etc).

  `OpencodeClient.create_session(directory, agent, model=None)` extended
  to accept the model struct as an optional third arg and pass it
  through in the POST body.

- **`reviewer.oneshot_open_pr(client, agent, base_branch, model_specs)`**
  replaces v0.14.6's `finalize_and_open_pr` slug+initial_prompt fallback
  in `_phase_committing`. Iterates `pr_fallback_models` and for each:

  1. `parse_model_id(spec)` (skipped with attempt-log entry if invalid)
  2. `client.create_session(worktree, agent="build", model=model)`
  3. Sends the new `oneshot_open_pr_prompt(branch, base_branch, initial_prompt)`
     to the fresh session (a focused PR-authoring prompt that mentions
     the staging-commit amend option and forbids `--fill`).
  4. Parses the response for the `PR_OPENED:` sentinel using
     `parse_pr_opened` (existing widened parser from v0.14.6).

  First successful sentinel wins. All models exhausted → agent
  transitions to `FAILED` with
  `last_error="all PR-fallback models exhausted: <attempts>"`.
  Returns `(PrInfo | None, attempts: list[str])` so the caller can
  produce the audit-log line.

- **31 new regression tests:**
  - `TestParseModelId` (7): two-part, three-part with variant, opencode
    provider, missing slash, empty, only-provider edge cases, whitespace
    stripping.
  - `TestPrFallbackModelsConfig` (4): defaults; YAML beats env; env-only
    comma-separated; empty YAML falls through to env to default.
  - `TestMessageIsRateLimited` (7): no-error returns None; non-APIError
    returns None; statusCode==429 matches; textual fallback when
    statusCode != 429; retry-after header parsed; retryAfterMs metadata
    parsed; non-rate-limit APIError (e.g. 401) returns None.
  - `TestCheckExecutorRateLimited` (4): no-error returns False;
    rate-limit transitions to RATE_LIMITED with phase_before saved;
    already-RATE_LIMITED is noop; min-wait floor applied when retry-after
    missing.
  - `TestPhaseRateLimited` (2): restores prior phase when retry-at
    elapsed; defaults to EXECUTING when no prior_phase saved.
  - `TestPhaseQueued` (3): drains when no blockers; remains QUEUED with
    blocked_by populated when RATE_LIMITED exists; blocked_by list
    self-heals across drift.
  - `TestOneshotOpenPr` (4): first model succeeds; first-fails-second-wins;
    all exhausted returns None with full attempt log; invalid model spec
    skipped without consuming a session create.

  Full suite: 314 passed (was 283 at v0.14.6).

### Changed

- **`_phase_committing`** no longer calls
  `reviewer.finalize_and_open_pr` as the fallback. New flow:
  `executor_open_pr` → if returns None and not rate-limited →
  `oneshot_open_pr(pr_fallback_models)` → if all exhausted → FAILED.
  The slug+initial_prompt fallback path is fully removed from the
  state-machine. `finalize_and_open_pr` survives as an importable
  module-level function for CLI / manual recovery use.

- **`PHASES` set** gained `QUEUED` and `RATE_LIMITED` (state.py).
- **`_PHASE_HANDLERS` dispatch table** gained `QUEUED → _phase_queued`
  and `RATE_LIMITED → _phase_rate_limited`.
- **`Agent` dataclass** gained 4 new fields: `rate_limited_at`,
  `rate_limit_retry_after_at`, `phase_before_rate_limit`,
  `queued_blocked_by`.
- **`Config.notify_events`** default set gained 4 new kinds:
  `rate_limited`, `rate_limit_cleared`, `queued`, `queue_drained`.
- **`_EVENT_GLYPH`** gained glyphs for the 4 new kinds (`⏳` for
  rate_limited/queued; `▶` for rate_limit_cleared/queue_drained).
- **`_default_event_body`** gained body templates for the 4 new kinds.

### AGENTS.md

- New `Rate limits and queue (LOAD-BEARING)` section documenting the
  detection signal, retry-after parsing, two-phase additions, queue
  gating, and the precedence rule (call
  `_check_executor_rate_limited` BEFORE `_check_executor_abort` in any
  new handler that touches the executor session).
- State-machine diagram updated to show the new phases.

### Known gaps (deferred to v0.15.x)

- Reviewer session rate-limit detection: if the reviewer hits a 429,
  `_phase_reviewing` does not yet route through the same RATE_LIMITED
  path. The fix is to factor `_check_executor_rate_limited` into a
  generic `_check_session_rate_limited(agent, session_id, worktree)`
  and call it from `_phase_reviewing` as well. Tracked for v0.15.1.

## [0.14.6] - 2026-05-17

### Fixed

- **PR title / body regression: executor-driven path falls back every
  time.** The `_phase_committing` flow has been "executor authors title
  + body, plugin parses `PR_OPENED:` sentinel, falls back to slug-based
  finalize on parse failure" since v0.4.0 (documented as LOAD-BEARING in
  AGENTS.md). In v0.14.x every PR was hitting the fallback because the
  executor wasn't emitting the sentinel — producing slug-derived titles
  like `Fix bb gateway` from agent_id `oco/fix-bb-gateway` and bodies
  set to the verbatim initial prompt.

  Three coordinated fixes:

  1. **`executor_open_pr_prompt(...)` strengthened**: explicit ban on
     `gh pr create --fill` (it would pull from the pre-review
     `chore: <slug>` staging commit and produce garbage), a concrete
     `PR_OPENED: https://github.com/octocat/hello-world/pull/42` example
     to copy the shape of, and an explicit note that the executor MAY
     amend the pre-review staging commit so final git history reflects
     what actually changed (instead of the slug placeholder).

  2. **`parse_pr_opened(text)` accepts three formats**: strict
     `PR_OPENED:` (primary), permissive
     `PR opened` / `Opened PR` / `PR url` variants (middle layer), and
     bare `github.com/.../pull/N` URLs (last-resort fallback). Widening
     catches drift in executor wording without losing strictness for
     compliant responses.

  3. **`executor_open_pr(...)` diagnostic logging**: on parse failure,
     logs the executor's full assistant response (truncated to 4KB) at
     WARNING level, so future drift is debuggable from the orchestrator
     log without re-running the failure. Also logs the successful
     response at INFO with the first 800 chars.

### Added

- **Tick-failure surfacing + auto-escalation.** Previously,
  `_record_tick_failure` recorded `last_tick_error` /
  `consecutive_tick_failures` but never fired a notification and never
  escalated the phase, so a stuck agent looped forever in EXECUTING
  with no user-visible signal. Now:

  - First failure of a streak fires the new `tick_error` notification
    (`"Tick failed: <summary>"`). Subsequent consecutive failures do
    NOT re-notify (avoids spam from transient network errors). A
    successful tick clears the streak.
  - After `TICK_FAILURE_ESCALATION_THRESHOLD = 3` consecutive failures
    the agent transitions to `phase=FAILED` with
    `last_error = "stalled after N consecutive tick failures: ..."`,
    fires the existing `failed` notification, and cancels its asyncio
    tasks. Terminal agents are not re-escalated.

  `tick_error` is in the default `notify_events` set.

- **Opencode-side abort detection + auto-continue.** Opencode marks an
  aborted assistant turn by setting `message.error = { name, message }`
  (e.g. `MessageAbortedError` / `"Interrupted"`) on the assistant
  message — a structured field, NOT a text part — so the existing
  text-part readers in event_loop.py never saw aborts. The agent would
  appear to be running while actually being stuck.

  New `_message_error(item)` extracts the structured error.
  `_check_executor_abort(agent)` runs from both `_phase_executing` and
  `_phase_executor_addressing` immediately after `wait_idle` succeeds.
  Behavior:

  - Idempotent on `message.id`: each new abort fires exactly one
    `aborted` notification and queues exactly one `continue` follow-up
    to the executor via `send_message_async`. Same-id re-observations
    are noops.
  - Forward progress (no error on latest message) clears
    `consecutive_aborts` and `last_abort_msg_id`.
  - After `ABORT_ESCALATION_THRESHOLD = 3` distinct aborts the agent is
    escalated to `phase=FAILED` with
    `last_error = "executor aborted N consecutive times: ..."`, fires
    the existing `failed` notification, and cancels asyncio tasks.

  `aborted` is in the default `notify_events` set. New `Agent` fields:
  `last_abort_msg_id: str | None`, `consecutive_aborts: int = 0`.

- **24 new regression tests** in `tests/test_pure_logic.py`:
  - `TestParsePrOpenedAcceptsVariants` (7): strict prefix; case
    insensitivity; PR-opened-with-space variant; Opened-PR variant;
    PR-url variant; bare github URL fallback; no-match returns None.
  - `TestExecutorOpenPrPromptHardening` (4): prompt forbids `--fill`;
    contains concrete `PR_OPENED:` example; emphasizes the literal
    prefix is REQUIRED; mentions the staging-commit amend option.
  - `TestMessageErrorExtraction` (5): no error returns None;
    MessageAbortedError extracted as `(name, message)`; error without
    message-string still extracted; error at item level also detected;
    error without `name` rejected as None.
  - `TestRecordTickFailureEscalation` (4): first failure fires
    `tick_error`; second consecutive does NOT re-notify; third
    consecutive escalates to FAILED; terminal agents not re-escalated.
  - `TestCheckExecutorAbort` (4): no error returns False AND clears
    streak; first abort notifies + sends "continue"; same `message.id`
    is idempotent (no re-notify, no re-send); third distinct abort
    escalates to FAILED.

  Full suite: 283 passed (was 259 at v0.14.5).

### Changed

- `notify_events` default set: added `tick_error` and `aborted` kinds.
  Both `Config.notify_events` and `Config.from_plugin_entry`'s
  `default_events` updated; users with explicit
  `plugins.entries.hermes-opencode.notify.events.enabled` lists must
  add these kinds manually to receive the new notifications.
- `_default_event_body` body templates added for `tick_error` and
  `aborted` (used when `_notify_event` is called without an explicit
  body).
- `_EVENT_GLYPH` gained `tick_error: "⚠"` and `aborted: "⏹"`.
- `AGENTS.md` gained a new `Error surfacing (LOAD-BEARING)` section
  documenting both surfaces (tick failures, message-level aborts) and
  their thresholds. The existing `Executor-driven PR open` section
  records the v0.14.x slug-fallback regression and the three fixes
  applied here.

## [0.14.5] - 2026-05-17

### Added

- **`opencode_server.serve_hostname` config knob.** New `Config.serve_hostname`
  field, configurable via
  `plugins.entries.hermes-opencode.opencode_server.serve_hostname` in
  `~/.hermes/config.yaml` or the `OPENCODE_SERVE_HOSTNAME` env var.
  When set, the value is passed to `opencode serve --hostname=...`
  instead of the host parsed out of `server_url`. Lets a user bind
  opencode to `0.0.0.0` (reachable from other hosts on the LAN) while
  keeping `server_url` on loopback for local connects. Default `None`
  preserves the v0.3.0+ behaviour (bind matches the connect URL host).

  `OpencodeClient.__init__` now takes an optional third `serve_hostname`
  argument and stores the connect host in `self._host` (used for the
  TCP readiness probe and as the httpx target) and the bind host in
  `self._serve_hostname` (used for the `--hostname=` spawn flag). The
  two diverge only when the knob is set; otherwise `serve_hostname`
  falls back to `_host`.

- **Dashboard surfaces the configured opencode serve URL** in the page
  header. Frontend pulls it from the new `/api/plugins/hermes-opencode/config`
  endpoint (and from the `server_url` field on `/agents`,
  `/agents/{id}`, and the WebSocket `snapshot` / `agents` payloads, so
  it stays in sync across transports without a separate fetch).

- **Per-agent session URLs in the dashboard.** Each agent row now
  carries a `session_url` (and `reviewer_session_url` when applicable),
  constructed as `<server_url>/session/<session_id>/message`. The
  agent-detail modal renders both as clickable anchors. The URL points
  at the opencode HTTP API endpoint that returns the session's message
  list as JSON; opening it in a browser without the
  `x-opencode-directory` header will 4xx, but the URL is the canonical
  copyable handle for `curl` / API inspection. Opencode has no HTML
  session UI, so the dashboard's own modal is the human-facing surface
  (click an agent row to open it).

- **15 new regression tests:**
  - `tests/test_pure_logic.py::TestServeHostnameConfig` (7 cases):
    default `serve_hostname` is `None`; YAML key reads through;
    `OPENCODE_SERVE_HOSTNAME` env var reads through; YAML overrides env;
    `OpencodeClient` default `_serve_hostname` falls back to URL host;
    explicit override doesn't change connect host; empty override falls
    back.
  - `tests/test_dashboard_ws.py::TestDashboardServerUrlAndSessionUrls` (8
    cases): `_make_session_url` constructs the URL correctly, strips
    trailing slash, returns `None` on missing inputs; `_inject_session_urls`
    handles reviewer rows + missing session_ids; `/agents` carries
    `server_url` + `session_url`; `/agents/{id}` carries `session_url`;
    `/config` returns the expected shape; WebSocket snapshot carries
    `server_url` + per-row `session_url`.

  Full suite: 259 passed, 0 skipped (was 242 at v0.14.4).

### Changed

- `dashboard/dist/index.js` rebuilt from `src/index.jsx` via
  `bun run build`. Source-of-truth remains `src/index.jsx`.
- `dashboard/dist/style.css` gained `.oco-server-url` + label rules.
- `AGENTS.md` gained a new `Dashboard API surface` section enumerating
  every endpoint and its JSON shape, and the `Sync vs async opencode
  endpoints` section now records the v0.14.5 `serve_hostname` knob.

## [0.14.4] - 2026-05-17

### Fixed

- **`oc_send` no longer blocks the hermes main session.** Same anti-pattern
  the v0.3.1 → v0.3.2 release fixed for `oc_spawn`, just on a different
  surface: `make_send`'s handler was awaiting `rt.client.send_message(...)`
  (the blocking `/session/:id/message` endpoint) with a default
  `timeout_sec=600`, so any chat-side `oc_send` froze the hermes main
  session for the full duration of opencode's reply stream. Users
  perceived this as "the message got queued" — subsequent messages
  couldn't be processed until the prior `oc_send` returned.

  Fix: `make_send` now awaits `rt.client.send_message_async(...)` (the
  `/session/:id/prompt_async` endpoint, ~30 ms POST) and returns
  `{"agent_id": ..., "queued": True, "note": ...}` immediately. The tool
  result no longer carries the agent's reply text. The bg event loop
  picks up the assistant reply via the SSE buffer + `get_messages`
  exactly as it does for `oc_spawn`, and the existing awaiting-input /
  pending-question notifications surface progress to the user.

  Schema change: `SEND_SCHEMA` dropped the `timeout_sec` parameter
  (it has no effect on the async queue endpoint, and the queue POST has
  its own 30 s ceiling inside `transport.py`). Description and `text`
  param description updated to make the async behaviour explicit.

  This brings `oc_send` into compliance with the `AGENTS.md` rule that
  has been load-bearing since v0.3.2: *any code path called synchronously
  by a hermes tool dispatcher must use `send_message_async`*.

### Added

- **`@<agent_id> <body>` direct gateway dispatch.** New shortcut in
  `_pre_gateway_dispatch_hook` (sibling to the existing `/oc` parser)
  forwards a message to a live agent's opencode session VERBATIM via
  `send_message_async`, fully bypassing the hermes chat LLM. Zero
  paraphrasing surface (chat LLM never sees the message), zero blocking
  on opencode's reply.

  Resolution semantics:
  - Message must start with `@`; agent_id must match the
    `[A-Za-z0-9][A-Za-z0-9_-]*/[A-Za-z0-9][A-Za-z0-9_-]*` charset (the
    `worktree.compose_agent_id` shape).
  - The agent_id must resolve to a known live agent. Unresolved
    `@user` mentions fall through silently so unrelated `@mentions` in
    group chats still reach the chat LLM normally.
  - Terminal-phase agents (`DONE` / `KILLED` / `FAILED` / `CANCELLED`)
    are rejected with `[hermes-opencode] cannot dispatch to @<id>:
    phase=<phase>`.
  - Empty body is rejected with `[hermes-opencode] empty message; use
    @<id> <text>`.
  - Valid dispatch echoes `[hermes-opencode] -> @<id>` back to the
    channel.

  See `AGENTS.md::Gateway @<agent_id> direct dispatch` for the full
  resolution table.

- **18 new regression tests** in `tests/test_pure_logic.py`:
  - `TestSendIsAsyncFireAndForget` (5 cases): `SEND_SCHEMA` no longer
    exposes `timeout_sec`; description documents the async / queued /
    `oc_status` / `oc_wait` semantics; v0.14.3 dispatcher wording
    preserved; `make_send` awaits `send_message_async` and never the
    blocking `send_message`; unknown-agent error path.
  - `TestAtAgentDirectDispatch` (13 cases): regex matches simple,
    mixed-case, and multiline bodies; regex rejects no-slash and
    mid-message `@…`; no-runtime / non-`@` / unknown-agent all fall
    through silently; terminal-phase and empty-body short-circuit with
    the documented reason strings; valid dispatch calls
    `send_message_async` and echoes confirmation; hook integration:
    `@` dispatch takes precedence over `/oc` and `/oc` continues to
    work when no `@` match.

  Full suite: 242 passed, 1 skipped (was 224 at v0.14.3).

### Changed

- `AGENTS.md` gained a `Gateway @<agent_id> direct dispatch
  (LOAD-BEARING)` section pinning the resolution semantics, and the
  `Sync vs async opencode endpoints` section now records the v0.14.3 →
  v0.14.4 regression history alongside the older v0.3.1 → v0.3.2 one.

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
