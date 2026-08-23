#!/usr/bin/env bash
# Re-capture every CC release since the last committed capture, snapshot each
# under its version, and leave captures/ on the newest. Runs identically
# locally and in CI.
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd -- "$repo_root"

uv run cc-prompts-refresh --repo "$repo_root"
