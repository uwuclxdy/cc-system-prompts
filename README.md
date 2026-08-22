# cc-system-prompts

Track Claude Code's per-model system prompts as reviewable diffs.

Claude Code builds its system prompt on the client, so the prompt a given model receives is observable without touching Anthropic's API. This repo captures that prompt per model, normalizes it, and commits it. A daily job re-captures and opens a PR when anything moved, which turns a silent upstream prompt change into a diff someone can read.

## What it captures

Two flavors exist, and the CLI picks between them by spawn shape:

| flavor | spawn | identity line | file |
|---|---|---|---|
| cli | interactive pty | `You are Claude Code, Anthropic's official CLI for Claude` | `captures/<model>.md` |
| sdk | `claude -p` | `You are a Claude agent, built on Anthropic's Claude Agent SDK` | `captures/<model>-sdk.md` |

A `claude -p` run marks the session non-interactive, so capturing the CLI flavor needs a real pty rather than a pipe. The capture tool forks one.

## How it works

No upstream API is involved. A stdlib HTTP server binds a loopback port, the CLI is pointed at it through `ANTHROPIC_BASE_URL` with a dummy key, and the server records each request's `system` blocks and answers 400. One rejected request per model is enough.

Each spawn gets a fresh config dir seeded with onboarding plus folder trust, and a temporary working directory, so no project's files or history are loaded as context. That is not the same as a context-free prompt: the temp dir follows `TMPDIR`, and if that path happens to sit inside a git checkout, Claude Code walks up, finds it, and stamps a `gitStatus:` block into the prompt.

So normalization, not isolation, is what makes a capture publishable. Before writing, the tool replaces everything machine-specific: dates, working directory, OS version, model line, knowledge cutoff, session ids, remaining-token lines, memory paths, and the whole `gitStatus:` body (branch names, git identity, working-tree paths, commit subjects). The block's labels survive, because their wording is upstream prompt text and a change to it is exactly what this repo tracks. No home path survives in either spelling.

Every capture carries a provenance header naming the observation date, the Claude Code version, and the model id.

## Layout

| path | role |
|---|---|
| `src/cc_prompts/` | the capture tool: recorder, spawn drivers, normalizer |
| `captures/` | current normalized captures, one file per model per flavor |
| `archive/<cc-version>/` | a snapshot of the whole set each time it changed |
| `scripts/refresh-captures.sh` | re-capture and archive; the same script CI runs |

## Running it

```sh
uv sync --frozen
uv run cc-prompts-capture                  # every model, both flavors
uv run cc-prompts-capture --models opus --mode cli
./scripts/refresh-captures.sh              # capture, then archive on drift
```

The capture must run against the real `claude` launcher. Pointing it at a wrapper that injects `--system-prompt-file` records that custom prompt instead of the stock one, and the script refuses a path under a `shims` directory for that reason.

## Caveats

A capture diff can move for two different reasons: Anthropic changed the prompt, or the runner picked up a new Claude Code version. The provenance header and the per-version archive separate them.

The prompt text here is Anthropic's, reproduced as observed for change tracking.
