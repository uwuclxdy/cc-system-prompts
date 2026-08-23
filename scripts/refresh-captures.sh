#!/usr/bin/env bash
# Re-capture every model's stock system prompt, and snapshot the set under its
# CC version whenever the capture changed. Runs identically locally and in CI.
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd -- "$repo_root"

claude_bin=${CLAUDE_BIN:-"$HOME/.local/bin/claude"}
if [ ! -x "$claude_bin" ]; then
    printf 'refresh-captures: no executable claude at %s; set CLAUDE_BIN\n' "$claude_bin" >&2
    exit 1
fi

# The shim would inject --system-prompt-file and the capture would record the
# custom prompt instead of the stock one. Always drive the real launcher.
case $claude_bin in
*/shims/*)
    printf 'refresh-captures: %s is the shim; captures must use the real binary\n' "$claude_bin" >&2
    exit 1
    ;;
esac

uv run cc-prompts-capture --claude-bin "$claude_bin" --out captures

# The subagent prompt is a separate artifact: it reaches the wire only when
# something spawns a subagent, so it needs its own probe. One file covers every
# claude model (all four normalize identical); deepseek gets its own, since a
# non-claude model is told its name differently and gets no cutoff line.
uv run cc-prompts-subagent --claude-bin "$claude_bin" --out captures/subagent.md
uv run cc-prompts-subagent --claude-bin "$claude_bin" --model deepseek-chat \
    --out captures/subagent-deepseek.md

if git diff --quiet -- captures/; then
    printf 'refresh-captures: no drift\n'
    exit 0
fi

# Provenance header shape: `observed <date> (wire capture, CC <version>, <model>)`
version=$(sed -n '1s/.*, CC \([0-9][0-9.]*\),.*/\1/p' captures/opus.md)
if [ -z "$version" ]; then
    printf 'refresh-captures: no CC version in the provenance header of captures/opus.md\n' >&2
    exit 1
fi

mkdir -p -- "archive/$version"
cp -- captures/*.md "archive/$version/"
printf 'refresh-captures: drift captured, archived under archive/%s\n' "$version"
