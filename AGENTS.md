# opencode-orchestrator — Developer Notes for AI Agents

Instructions for AI coding assistants working on this hermes-agent plugin.
Read this **before** editing. Conventions here are load-bearing — violating
them silently breaks behaviour the test suite doesn't cover.

## What this is

A hermes-agent plugin (Python, in-process) that orchestrates multiple
opencode agents in git worktrees. Plugin loads at hermes startup, registers
18 tools + 3 lifecycle hooks + 1 dashboard tab, spawns a singleton bg
asyncio loop that drives a per-agent state machine through executor →
reviewer → commit → PR_OPEN → DONE.

## Plugin runtime contract

The plugin is loaded by hermes' `PluginManager` via
`importlib.util.spec_from_file_location` with `submodule_search_locations`
set to the repo root. Concretely:

- `__init__.py` is loaded as `hermes_plugins.opencode_orchestrator`
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
all live under `~/.hermes/plugins/opencode-orchestrator/`. JSON file
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
- **Marketing comments, AI-slop narration, em-dashes in code.** No.
- **New top-level helpers files like `utils.py` / `helpers.py`.** Module
  names must describe what they own.

## Files at the repo root and what they own

| File | Owns |
|---|---|
| `__init__.py` | `register(ctx)` — entry point; wires tools + hooks + event loop + notify inject_message binding |
| `config.py` | `Config` dataclass; reads plugin entry from `~/.hermes/config.yaml`; paths under `~/.hermes/plugins/opencode-orchestrator/` |
| `transport.py` | `OpencodeClient` — async httpx wrapper around opencode HTTP API; both `send_message` (sync) and `send_message_async` (queue) |
| `worktree.py` | `git worktree` ops, `project_key_for`, `derive_abbrev`, `compose_agent_id`, `slugify` |
| `projects.py` | `ProjectRegistry` over `projects.json` |
| `state.py` | `AgentStore` over `agents.json`; `Agent` dataclass; `PHASES` set |
| `tools.py` | 18 tool schemas + handlers + `all_tool_specs(rt)`; `Runtime` dataclass |
| `event_loop.py` | Singleton bg asyncio loop + per-agent state machine + pruner + heartbeat scheduler |
| `bootstrap.py` | Shell-bash extraction from skill SKILL.md; opencode-driven recovery on failure; `generate_bootstrap_skill` |
| `reviewer.py` | Sister-worktree staging, `REVIEW: LGTM/REQUESTS_CHANGES` classifier, `finalize_and_open_pr` |
| `pr.py` | `gh pr create --fill` + `gh pr view --json` wrappers; `PrInfo` dataclass |
| `notify.py` | Sink fanout (CLI `inject_message`, gateway DM via `platform_registry.create_adapter`, dashboard JSONL append) |
| `heartbeat.py` | Hourly report builder; phase glyphs; TZ-aware day window; `_format_age`; `next_top_of_hour` |
| `dashboard/manifest.json` | Plugin manifest read by hermes' dashboard discovery |
| `dashboard/plugin_api.py` | FastAPI router (READ-ONLY; mounted at `/api/plugins/opencode-orchestrator/`) |
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
