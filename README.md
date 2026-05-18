# hermes-opencode

A [hermes-agent](https://github.com/NousResearch/hermes-agent) plugin that drives multiple
[opencode](https://opencode.ai) agents running in parallel git worktrees. Spawn an agent, hand it a
verbatim prompt, and the plugin runs the full **executor -> reviewer -> PR** cycle in the background.
Pull requests open via `gh`. Human-in-the-loop questions and permission requests surface as
DM notifications (or hermes CLI messages) and route back to the right opencode session by inference.
An awaiting-input classifier protects the reviewer from racing into incomplete work when the
executor stops to ask for clarification.

Resilience (v0.18.0+): every recoverable error path runs a per-phase retry budget before
escalating; truly stuck agents land in `NEEDS_INTERVENTION` (not `FAILED`) so operators can fix
the root cause and run `oc_retry` to resume. Hourly heartbeats, phase-stuck warnings, and
state-transition events keep you informed via your preferred channel (CLI, dashboard, or
gateway DM — auto-detected from your hermes home channel).

Proactive chat UX (v0.19.0+): hermes sees every active agent's phase, session status, latest
output snippet, and any events that fired since your last message — so it dispatches obsessively
when you ask for code work and narrates progress without polling. Serve-crash post-mortems
(v0.20.0+) capture exit codes, signal names (e.g. `SIGKILL`), uptime, and 20 lines of dying log
into `serve_crashes.jsonl` for forensic recovery after `opencode serve` flaps.

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
        # opencode_server takes host + port. The connect URL is built
        # internally as `http://{host}:{port}`. `opencode serve` is
        # spawned with `--hostname={host} --port={port}` (opencode's
        # CLI does not accept --url). Set host to 0.0.0.0 to expose
        # serve to other machines on the LAN.
        host: "127.0.0.1"
        port: 4096
        password: "${OPENCODE_SERVER_PASSWORD}"
        # When the executor session fails to author its own PR title +
        # body (no `PR_OPENED:` sentinel), the plugin spawns a fresh
        # opencode session per fallback model in this list and asks it
        # to author the PR. First success wins. All exhausted -> agent
        # routes to NEEDS_INTERVENTION (v0.18.0+) instead of FAILED, so
        # an operator can fix gh auth / network and run `oc_retry`.
        # Default list deliberately avoids Anthropic models because
        # Claude is the most common rate-limit victim.
        pr_fallback_models:
          - "openai/gpt-5.5"
          - "opencode/deepseek-v4-flash-free"
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
          # v0.14.6 added tick_error + aborted. v0.15.0 added rate_limited,
          # rate_limit_cleared, queued, queue_drained. v0.16.0 added
          # awaiting_human_resumed. v0.18.0 added needs_intervention,
          # phase_stuck. v0.19.0 added progress_narration (off by default
          # via the toggle below; the event kind itself is enabled here
          # so that turning narration on actually delivers).
          enabled: ["pr_opened", "done", "failed", "awaiting_human",
                    "awaiting_human_resumed", "review_started", "cancelled",
                    "tick_error", "aborted", "rate_limited", "rate_limit_cleared",
                    "queued", "queue_drained", "needs_intervention",
                    "phase_stuck", "progress_narration"]
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
      # v0.19.0: optional periodic "still working" DMs for non-blocked
      # agents whose SSE buffer changed since the last fire. Off by
      # default; enable for unprompted progress narration.
      progress_narration:
        enabled: false
        interval_sec: 300
        snippet_chars: 280
```

Serve-side knobs (v0.20.0+) live at the top level of `Config`:

- `serve_crashes_file` — JSONL audit log of every detected `opencode serve` crash / failed
  restart (default `~/.hermes/plugins/hermes-opencode/serve_crashes.jsonl`).
- `serve_log_retention_count` — keep the newest N timestamped serve logs in
  `~/.hermes/plugins/hermes-opencode/logs/` (default 50). Older ones are pruned by the
  hourly cleanup loop.

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

Events that fire notifications: `pr_opened`, `done`, `failed`, `awaiting_human`,
`awaiting_human_resumed` (v0.16.0+, fires when the AWAITING_HUMAN gate clears), `review_started`,
`cancelled`, plus `tick_error` (v0.14.6, transient executor failure surfaced on first occurrence
— v0.19.1+ suppresses the consecutive-failure counter while `opencode serve` is unhealthy so
agents aren't FAILED for the supervisor's own restart cycle), `aborted` (v0.14.6, opencode-side
tool-execution-aborted; v0.18.0+ uses three progressively-escalating nudges instead of three
identical `continue` pokes), `rate_limited` / `rate_limit_cleared` (v0.15.0+, provider 429
detected and wait-and-resume), `queued` / `queue_drained` (v0.15.0+, new spawns parked while
rate-limited agents clear), `needs_intervention` (v0.18.0+, recoverable failure routed to the
non-terminal NEEDS_INTERVENTION phase awaiting an operator + `oc_retry`), `phase_stuck`
(v0.18.0+, agent stuck in a transitional phase for > 10 min), and `progress_narration`
(v0.19.0+, off by default; opt-in periodic "still working" pings).

Disable any subset by reducing `notify.events.enabled`. The `serve_down` event (opencode-server
crash) always fires to `cli`, `dashboard`, and `gateway` regardless of config. v0.20.0+ embeds
the captured exit code, signal name, uptime, and last 5 log lines directly into the
`serve_down` notification body for inline diagnosis.

### Awaiting-input classifier

When the executor goes idle with worktree changes but with no formal `/question` or `/permission`
entry, the plugin can't tell from opencode's message protocol alone whether the executor finished
or is waiting on a plain-text "which option do you prefer?" prompt. A multi-layer cascade gates
the `EXECUTING -> IDLE_TASK_COMPLETE -> REVIEW_SPAWNING` transition to avoid running the reviewer
against incomplete work:

1. **Regex layer** (always on, free): 10 patterns covering trailing `?`, which-option phrasing,
   should-I, would-you-prefer, let-me-know, please-confirm, y/n, awaiting-your-input, and labeled
   options ("Option A:", "Option B:").
2. **Todo-override** (v0.17.0+): if the opencode `todowrite` tool's latest call still has any
   non-completed todo, the cascade short-circuits with `awaiting=False` regardless of regex hits.
   Any in-flight todo proves the executor has work to do, so prose the regex flagged is more
   likely status narration than a real question.
3. **LLM layer** (configurable): calls hermes-agent's auxiliary client
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
   `classifier.enabled: false` to run regex-only. v0.17.0+ also filters reasoning / thinking
   parts and user-role messages out of the classifier input — only assistant text reaches the
   model.
4. **Stalled-idle reminder loop**: re-notifies `awaiting_human` for any agent that's been
   waiting longer than `awaiting_input.reminder_interval_sec` (default 30 min).

When the cascade detects awaiting, the agent transitions to the `AWAITING_HUMAN` phase (v0.16.0+,
a distinct non-terminal phase, not a flag on EXECUTING), saving `phase_before_awaiting` for clean
resumption. A context-rich DM goes out with the last assistant text snippet. The reviewer is
never run against incomplete work.

The executor is also instructed via the `[SYSTEM DIRECTIVE: HERMES-OPENCODE - ORCHESTRATOR RULES]`
envelope (v0.14.0+) to use the `/question` API as the authoritative signal — the classifier is
the safety net for noncompliance, not the primary path. v0.17.0+ also instructs the executor to
emit a `READY_FOR_REVIEW` sentinel on its own line when it's actually done, so the orchestrator
can transition immediately without waiting for the 120 s idle debounce.

The opencode SSE feed also publishes a canonical `session.status` ("idle" / "busy" / "retry") per
agent (v0.17.0+); the idle-detection gate consults that server-side signal first before any
heuristic.

## Tools

All 24 tools are exposed to the LLM under the `hermes_opencode` toolset. Names map 1:1 to their
underlying handler in [`tools.py`](./tools.py). Tool descriptions carry "WHEN TO USE" leads
(v0.19.0+) so the hermes chat LLM picks the right tool on the first try.

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
| `oc_resume_pr` | Resume work on an EXISTING open PR (v0.16.0+): check out its branch in a fresh worktree, spawn a session, forward the follow-up `prompt` verbatim. `skip_review=true` for trivial follow-ups. |
| `oc_send` | Send a message to a live agent |
| `oc_status` | Show one agent or all agents. v0.19.0+ includes `session_status` (busy/idle/retry), `last_assistant_text_snippet`, `last_classifier` verdict, `idle_since`, `phase_entered_at`. |
| `oc_wait` | Block until an agent goes idle |
| `oc_kill` | Abort agent's session; record erased from `agents.json` |
| `oc_cancel` | Wind down an agent without merging; record kept as `CANCELLED` with a reason |
| `oc_retry` | (v0.18.0+) Kick an agent to retry. Three modes: FAILED -> restore `phase_before_failed`; NEEDS_INTERVENTION -> restore `phase_before_intervention`; any other non-terminal phase -> reset retry counters (force re-tick after gateway restart or transient outage). |
| `oc_answer` | Forward the user's verbatim answer to a pending opencode `/question` or `/permission` |
| `oc_output` | Return the agent's latest assistant text from the live SSE buffer (with `/message` pull fallback) |
| `oc_review_now` | Trigger the reviewer immediately (skip the idle-debounce + awaiting-input gate) |
| `oc_review_again` | Re-run the reviewer on the executor's current state |
| `oc_skip_review` | Bypass review and go straight to COMMITTING / PR open |
| `oc_pr_status` | Poll GitHub for the PR's current state |
| `oc_set_notify_target` | Update `notify.gateway.{platform,chat_id}` at runtime without restarting hermes |
| `oc_heartbeat_send_now` | Force the hourly heartbeat report to fire immediately |
| `oc_serve_crashes` | (v0.20.0+) Return the last N rows from `serve_crashes.jsonl` — exit code, signal name, log tail, agents active at crash. Use proactively when narrating a `serve_down` event or answering "why did opencode die?". |

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
| `/oc retry <agent_id>` | (v0.18.0+) Kick an agent: FAILED -> restore `phase_before_failed`, NEEDS_INTERVENTION -> restore `phase_before_intervention`, otherwise reset retry counters. |
| `/oc doctor` | Single-message plugin health report (versions, bg loop, deps, state files, notify config, classifier config, per-agent awaiting-input verdicts, last events). Paste into bug reports. |
| `/oc test-notify [message ...]` | Force a full notify fanout (gateway DM + dashboard + cli) with a synthetic event; prints per-sink `[ok]` / `[FAIL]` with detail. For verifying the gateway DM trigger end-to-end. |
| `@<agent_id> <text>` | **v0.14.4 direct dispatch.** Forwards `<text>` VERBATIM to the agent's opencode session, fully bypassing the hermes chat LLM. Zero paraphrasing surface, zero latency. Unknown `@<id>` falls through to the chat LLM so unrelated `@mentions` in group chats are never eaten. Terminal-phase agents and empty bodies get a one-line rejection echoed back. |

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
hermes oco retry oco/refunds                            # v0.18.0+
hermes oco projects
hermes oco spawn <project> <task> <prompt ...>          # v0.16.0+
hermes oco resume-pr <project> <pr_number> <prompt ...> # v0.16.0+
hermes oco serve-crashes [--limit N] [--json]           # v0.20.0+
```

`kill` removes the worktree by default; pass `--keep-worktree` to retain it.
`cancel` runs the same teardown as `/oc cancel` but keeps the record as `CANCELLED` (audit).
`retry` is the canonical post-restart recovery surface; it picks the right resume mode
automatically based on the agent's current phase.

## Agent naming

Agent ids are `<abbrev>/<task>`, max 20 chars total:

- `abbrev` is auto-derived from the project label (`dodo-payments` → `dp`,
  `dodo-backend-bookings` → `dbb`, `payments` → `pay`) or set explicitly
  via `oc_project_add(abbrev=...)`.
- `task` is caller-supplied and slugified to kebab-case.
- Collisions append `-2`, `-3`, … with the task slug trimmed to fit.

## State machine

```
CREATED -> QUEUED? -> BOOTSTRAPPING -> EXECUTING -> AWAITING_HUMAN? ->
IDLE_TASK_COMPLETE -> REVIEW_SPAWNING -> REVIEWING ->
REVIEW_DELIVERED -> EXECUTOR_ADDRESSING -> AWAITING_HUMAN? ->
IDLE_REVIEW_ADDRESSED -> COMMITTING -> PR_OPEN -> DONE

(every phase can transition into RATE_LIMITED or NEEDS_INTERVENTION
 and resume back to its saved prior phase)
```

Terminal: `DONE` (merged, archived after 12 h), `CANCELLED` (manual / auto on PR closed, archived
after 12 h), `KILLED` (hard-deleted), `FAILED` (kept indefinitely until manually killed or
recovered via `oc_retry`). Non-terminal but blocking: `AWAITING_HUMAN`, `NEEDS_INTERVENTION`,
`RATE_LIMITED`, `QUEUED`.

`EXECUTING -> IDLE_TASK_COMPLETE` requires all of: opencode reports `session.status.type == "idle"`
(v0.17.0 SSE signal), no pending `/question` or `/permission`, worktree has uncommitted or
unpushed changes, EITHER the executor emitted the `READY_FOR_REVIEW` sentinel OR 120 s of
wall-clock idleness has elapsed (v0.17.0+, was 30 s before), and the awaiting-input cascade does
not flag the last assistant text as a question. The READY_FOR_REVIEW sentinel bypasses the
debounce entirely when present.

**v0.15.0 added two phases:**

- `QUEUED` — entered at `oc_spawn` time when any non-terminal agent is in `RATE_LIMITED`. The
  worktree, session, and bootstrap run normally; only the initial prompt is deferred. Drains to
  `EXECUTING` once all rate-limited agents clear.
- `RATE_LIMITED` — entered when the executor (or reviewer, since v0.15.1) hits a provider 429
  (Anthropic Claude is the typical victim). The plugin records the `retry-after` window, fires
  the `rate_limited` notification, parks the agent. Once the window elapses, the agent restores
  its saved prior phase and continues through its own normal flow. Review is NOT bypassed — the
  rate-limited task goes through whichever phase it was in.

**v0.16.0 added one phase:**

- `AWAITING_HUMAN` — entered whenever the awaiting-input cascade fires (pending `/question` or
  `/permission` on the executor session OR the classifier flags the latest assistant text as a
  prose question). Saves `phase_before_awaiting`. The dedicated `_phase_awaiting_human` handler
  polls each tick; when both detectors agree the agent is no longer awaiting (and forward
  progress has been observed since the entry message), it restores the saved phase and fires
  `awaiting_human_resumed`.

**v0.18.0 added one phase:**

- `NEEDS_INTERVENTION` — sibling of `AWAITING_HUMAN` for failures where the orchestrator can't
  proceed and needs an operator decision. Routed by `_handle_phase_failure(..., on_exhausted_intervene=True)`
  when a recoverable error path exhausts its retry budget AND has a clear operator action. The
  canonical example: `_phase_committing` PR-fallback exhausted (all models in
  `pr_fallback_models` returned no `PR_OPENED:` sentinel — usually means `gh auth status` is
  broken or github is unreachable). The agent waits at `NEEDS_INTERVENTION` until an operator
  fixes the underlying issue and runs `oc_retry`.

## Failure recovery (v0.18.0+)

Three tiers, in order of preference:

1. **Per-phase retry budget.** Every recoverable error path calls
   `_handle_phase_failure(agent, phase, summary)` instead of escalating to FAILED on first
   error. `PHASE_RETRY_CEILING` (per-phase) and `PHASE_RETRY_CEILING_DEFAULT = 3` govern the
   budget. The counter (`Agent.phase_retry_count`) auto-resets on every phase change, so the
   budget is naturally scoped per-phase without per-site bookkeeping. Six FAILED sites converted:
   QUEUED first-prompt send, REVIEW_SPAWNING staging + session create, REVIEWING "reviewer
   state lost" (re-stages instead of failing), REVIEW_DELIVERED dispatch, COMMITTING PR exhaust.

2. **`NEEDS_INTERVENTION`.** Recoverable failures with a clear operator action route to this
   non-terminal phase instead of FAILED, carrying `intervention_reason`. Operator fixes the
   root cause, runs `oc_retry`, agent restores `phase_before_intervention` and resumes.

3. **`oc_retry`.** Single tool / `/oc retry` / `hermes oco retry` for all three modes:
   FAILED -> `phase_before_failed`; NEEDS_INTERVENTION -> `phase_before_intervention`; any
   other non-terminal phase -> reset retry counters (useful for post-restart force-re-tick).
   Refuses on DONE / KILLED / CANCELLED, and on FAILED with `last_error` containing "project gone".

**Phase-stuck watchdog.** `_phase_stuck_loop` runs every 60 s; if an agent is in a transitional
phase (CREATED, BOOTSTRAPPING, IDLE_TASK_COMPLETE, REVIEW_SPAWNING, REVIEW_DELIVERED,
IDLE_REVIEW_ADDRESSED, COMMITTING) longer than 10 min, fires a one-shot `phase_stuck`
notification. Long-running phases (EXECUTING, REVIEWING, PR_OPEN) and blocked phases
(AWAITING_HUMAN, NEEDS_INTERVENTION, RATE_LIMITED, QUEUED) are skipped.

**Differentiated abort nudges (v0.18.0+).** `_check_executor_abort` (3-strike
MessageAbortedError detector) now varies the prompt by attempt:
`continue` -> "you stopped mid-task" -> `[SYSTEM DIRECTIVE: HERMES-OPENCODE - RESUME]`. Same
3-strike escalation, three different prompt shapes.

**Serve-down grace window (v0.19.1+).** Tick failures that occur while `_serve_is_unhealthy_or_healing()`
returns True (watchdog reports `serve_down`, or within `SERVE_HEALING_GRACE_SEC = 30 s` after
`serve_recovered`) still notify ONCE per agent per unhealthy episode but DO NOT increment
`consecutive_tick_failures` and never escalate to FAILED. Prevents the supervisor's own
restart cycle from FAILING active agents for transport errors that are unavoidable during
the flap. Healthy stalls still escalate at the 3-strike threshold.

## Hermes chat UX (v0.19.0+)

The hermes chat LLM receives a five-block context appended to every user message via the
`pre_llm_call` hook. Context goes into the USER message (not the system prompt), so the
system prompt stays identical across turns and Anthropic / OpenAI prompt-cache prefixes
keep hitting. All blocks are suppressed when they have nothing to say; cost is zero on quiet
turns.

Block order (directives first, state second, so directives aren't buried under state noise):

1. **`_DISPATCHER_DIRECTIVE`** — imperative MANDATORY RULES framing: new task -> `oc_spawn`
   verbatim; follow-up -> `oc_send`; status -> `oc_status` / `oc_output`; stuck -> `oc_retry`.
   Reaffirms that opencode owns its task and hermes is a dispatcher, never a planner.
2. **Active agents block** — one line per non-terminal agent: phase, session_status
   (idle/busy/retry from `_sse_session_status`), in_phase_for, optional pr_url, plus a
   220-char tail of the executor's latest assistant text from the live SSE buffer.
3. **"Since your last message" block** — per-hermes-session watermarks
   (`_session_watermarks`) advance every turn; tail of `events.log` filtered to entries newer
   than the last turn is rendered here. First call per chat session seeds the watermark so a
   fresh chat doesn't get blasted with historical backlog.
4. **DISPATCH NUDGE** — when `_looks_like_task(user_message)` matches an imperative verb
   (build, fix, implement, add, create, change, refactor, write, migrate, port, ship, wire,
   ... 30+). Suppressed on questions or oversize messages.
5. **ANSWER NUDGE** — when the user's reply token-matches an option label of a pending
   `/question` (or a yes/no token with exactly one question pending). Routes the LLM to call
   `oc_answer(question_id=..., answer=...)` instead of replying conversationally.

**Optional progress narration.** Enable via
`plugins.entries.hermes-opencode.progress_narration.enabled: true`. Every
`progress_narration_interval_sec` (default 5 min), the `_progress_narration_loop` checks each
non-terminal non-blocked agent's SSE buffer; if its tail snippet differs from the last fired one
(dedupe via `_last_narrated_snippets`), fires a `progress_narration` event. In gateway mode this
becomes unprompted "still working" DMs.

## Serve post-mortem (v0.20.0+)

Every detected `opencode serve` crash appends a structured row to
`~/.hermes/plugins/hermes-opencode/serve_crashes.jsonl`:

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
  "log_tail": "...last 20 lines of stdout+stderr...",
  "sec_since_last_alive": 60.3,
  "agents_active": ["BCK/p-list-tests", "BCK/prod-shortlink"]
}
```

Writers: the watchdog on ping failure, AND `_try_restart_serve_with_backoff` per failed attempt
(so "5 attempts failed" becomes a per-attempt breakdown with cause). `OpencodeClient.last_exit_info()`
classifies the exit as `still_running` / `clean_exit` / `nonzero_exit` / `killed_by_signal` /
`unknown_already_reaped`; `signal_name` recognizes both POSIX negative-returncode (`-9`) and
shell-style positive (`137`).

The next `serve_down` notification embeds `exit_kind`, `signal_name`, `pid`, `uptime`, and last
5 log lines directly into the body for inline diagnosis. Inspection surfaces:

- Tool: `oc_serve_crashes(limit=10)` — hermes calls proactively when narrating a `serve_down`.
- CLI: `hermes oco serve-crashes [--limit N] [--json]`.
- Dashboard: `GET /api/plugins/hermes-opencode/serve-crashes?n=20`.

`_prune_serve_logs` runs from the hourly cleanup loop and keeps the newest
`serve_log_retention_count` (default 50) timestamped serve logs in `logs/`.

## PR-creation fallback

The default path is **executor-driven**: after review LGTM, the orchestrator asks the executor
session to author its own PR title + body and run `gh pr create`. The executor emits
`PR_OPENED: <url>` on its own line so the plugin can capture the PR number.

When the executor fails to emit the sentinel (most often: it tried `gh pr create --fill` despite
being told not to, or it hit a non-recoverable provider error), v0.15.0 routes through
`oneshot_open_pr` — a fresh opencode session in the same worktree with an explicit non-Anthropic
model from `opencode_server.pr_fallback_models`. Iterates the list until one succeeds; if all
models exhausted the agent escalates to `FAILED` with the full per-model attempt log in
`last_error`. The old slug-derived-title + verbatim-initial-prompt fallback was removed in
v0.15.0 — that fallback produced consistently low-quality PR titles and bodies.

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
- `serve_crashes.jsonl` — (v0.20.0+) structured `opencode serve` crash records (exit code, signal, log tail, agents active)
- `wt/<agent_id_fs>/` — git worktrees (one per agent; `/` in agent id encoded as `__`)
- `wt/<agent_id_fs>.review/` — reviewer's sister worktree (auto-staged + torn down)
- `logs/opencode-serve.YYYYMMDD-HHMMSS.log` — per-spawn `opencode serve` stdout+stderr (newest 50 kept; older pruned hourly)
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
| ✓ | Event-loop resilience · opencode-serve forensics + watchdog | v0.14.1 |
| ✓ | Crash fix for `_last_assistant_text` name collision after the v0.14.0 SSE refactor | v0.14.2 |
| ✓ | Dispatcher discipline for `oc_spawn` / `oc_send` (verbatim-forwarding tool descriptions + per-turn `pre_llm_call` directive) | v0.14.3 |
| ✓ | `oc_send` async migration (no more hermes-session blocking) + `@<agent_id> <text>` direct gateway dispatch (bypasses chat LLM) | v0.14.4 |
| ✓ | Configurable `opencode_server.serve_hostname` + dashboard surfaces serve URL + per-agent session URLs | v0.14.5 |
| ✓ | Executor PR-title regression fix + tick-failure escalation + opencode-side abort detection with auto-`continue` | v0.14.6 |
| ✓ | Rate-limit detection (executor) + wait-and-resume + `QUEUED` spawn gate + non-Anthropic `pr_fallback_models` oneshot | v0.15.0 |
| ✓ | Rate-limit detection generalized to the reviewer session too | v0.15.1 |
| ✓ | `/oc attach` and `/oc cancel` no longer crash from gateway/TUI dispatch (new `event_loop.run_blocking` helper) | v0.15.2 |
| ✓ | `AWAITING_HUMAN` phase + `/oc spawn` + `oc_resume_pr` + opencode_server host rename | v0.16.0 / v0.16.2 / v0.16.4 |
| ✓ | READY_FOR_REVIEW sentinel · `session.status` SSE tracking · 120 s idle debounce · todo-override · reasoning/user-text leak fix | v0.17.0 |
| ✓ | Per-phase retry budgets · `NEEDS_INTERVENTION` phase · `oc_retry` tool/slash/CLI · phase-stuck watchdog · differentiated abort nudges | v0.18.0 |
| ✓ | Proactive chat UX: dispatcher directive · active-agents block · "since your last message" delta · richer `oc_status` · optional `progress_narration` · `oc_answer` nudge | v0.19.0 |
| ✓ | Serve-down grace window: tick failures during opencode-serve flap no longer escalate to FAILED | v0.19.1 |
| ✓ | Serve post-mortem: `serve_crashes.jsonl` · exit-code + signal capture · annotated `serve_down` body · `oc_serve_crashes` tool/CLI/dashboard · serve-log retention | v0.20.0 |

Future work lives as GitHub issues. PRs welcome.
