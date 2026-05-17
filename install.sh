#!/usr/bin/env bash
set -euo pipefail

# Local-dev install: symlink this repo root into the user's hermes plugins
# directory so hermes discovers it as a standalone directory plugin.
# For end users, prefer `hermes plugins install <owner>/<repo>` which clones
# from git into the same target path. Idempotent.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-${HOME}/.hermes}"
TARGET_DIR="${HERMES_HOME}/plugins/opencode-orchestrator"

if [[ ! -f "${REPO_DIR}/plugin.yaml" || ! -f "${REPO_DIR}/__init__.py" ]]; then
    echo "plugin.yaml or __init__.py not found at ${REPO_DIR}" >&2
    exit 1
fi

mkdir -p "${HERMES_HOME}/plugins"

if [[ -L "${TARGET_DIR}" || -e "${TARGET_DIR}" ]]; then
    rm -rf "${TARGET_DIR}"
fi

ln -s "${REPO_DIR}" "${TARGET_DIR}"

echo "linked ${TARGET_DIR} -> ${REPO_DIR}"
echo
echo "next: enable via"
echo "  hermes plugins enable opencode-orchestrator"
echo "or in ~/.hermes/config.yaml:"
echo "  plugins:"
echo "    enabled:"
echo "      - opencode-orchestrator"
