# hermes-opencode

Installed. To activate:

```bash
hermes plugins enable hermes-opencode
```

Or add to `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - hermes-opencode
```

## Runtime dependencies

This plugin depends on these Python packages (installed alongside hermes-agent):

- `httpx`
- `httpx-sse`
- `PyYAML`
- `opencode-api` (typed Python SDK for opencode, pinned to the opencode server version)

If any are missing in your hermes Python environment, install them:

```bash
pip3 install httpx httpx-sse PyYAML
pip3 install "git+https://github.com/that-ambuj/opencode-python-sdk.git@v1.15.5"
```

Or with `uv`:

```bash
uv pip install httpx httpx-sse PyYAML
uv pip install "git+https://github.com/that-ambuj/opencode-python-sdk.git@v1.15.5"
```

The opencode-api version pin tracks the opencode server version exactly.
If you upgrade your opencode server, bump the tag in
`requirements.txt` to match.

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

State lives under `~/.hermes/plugins/hermes-opencode/`.

Optional configuration in `~/.hermes/config.yaml`:

```yaml
plugins:
  entries:
    hermes-opencode:
      opencode_server:
        url: "http://127.0.0.1:4096"
      pr:
        base_branch: main
```
