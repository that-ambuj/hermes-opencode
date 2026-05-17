# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
