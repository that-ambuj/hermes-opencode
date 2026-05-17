# opencode-orchestrator

Installed. To activate:

```bash
hermes plugins enable opencode-orchestrator
```

Or add to `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - opencode-orchestrator
```

## Runtime dependencies

This plugin depends on these Python packages (installed alongside hermes-agent):

- `httpx`
- `httpx-sse`
- `PyYAML`

If any are missing in your hermes Python environment, install them:

```bash
pip install httpx httpx-sse PyYAML
```

You also need the `opencode` binary on `PATH`. The plugin will auto-spawn
`opencode serve` at first use if no server is reachable.

## First steps

```text
# Register a project
oc_project_add(label="my-app", repo_path="/path/to/repo")

# Spawn an agent
oc_spawn(project="my-app", task="add-login", prompt="Implement email/password login.")

# Watch it
oc_status()
oc_wait(agent_id="ma/add-login")

# Tear down
oc_kill(agent_id="ma/add-login")
```

State lives under `~/.hermes/plugins/opencode-orchestrator/`.

Optional configuration in `~/.hermes/config.yaml`:

```yaml
plugins:
  entries:
    opencode-orchestrator:
      opencode_server:
        url: "http://127.0.0.1:4096"
      pr:
        base_branch: main
```
