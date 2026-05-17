# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.3.0]: https://github.com/that-ambuj/opencode-orchestrator/releases/tag/v0.3.0
