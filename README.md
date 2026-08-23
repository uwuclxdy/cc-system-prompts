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

So normalization, not isolation, is what makes a capture publishable. Before writing, the tool replaces everything machine-specific: dates, working directory, OS version, model line, knowledge cutoff, session ids, remaining-token lines, memory paths, and the whole `gitStatus:` body (branch names, git identity, working-tree paths, commit subjects). The block's labels survive, because their wording is upstream prompt text and a change to it is exactly what this repo tracks.

No home path survives, in either the slash-separated or the dash-encoded spelling. A bare account name is caught separately, since it arrives with no path around it for a path-shaped rule to match.

Every capture carries a provenance header naming the observation date, the Claude Code version, and the model id.

## Layout

| path | role |
|---|---|
| `src/cc_prompts/` | the capture tool: recorder, spawn drivers, normalizer |
| `captures/` | current normalized captures, one file per model per flavor |
| `archive/<cc-version>/` | a snapshot of the whole set each time it changed |
| `scripts/refresh-captures.sh` | re-capture and archive; the same script CI runs |
| `shim/claude` | PATH wrapper that puts a custom system prompt in front of every spawn |

## Running it

```sh
uv sync --frozen
uv run cc-prompts-capture                  # every model, both flavors
uv run cc-prompts-capture --models opus --mode cli
./scripts/refresh-captures.sh              # capture, then archive on drift
```

The capture must run against the real `claude` launcher. Pointing it at a wrapper that injects `--system-prompt-file` would record that custom prompt instead of the stock one, so the capture is refused when its text matches the custom prompt's. `refresh-captures.sh` rejects a launcher path under a `shims` directory before any capture runs.

## The shim

`shim/claude` is a PATH wrapper that hands the real launcher a `--system-prompt-file`. That flag replaces the entire stock system array rather than the prose around the harness blocks, so `# Environment` goes with everything else and a session is left knowing neither its working directory, its platform, nor its OS version. The shim rebuilds that block from what the machine can answer and appends it to the prompt it passes. The stock block's other lines are upstream prose: the knowledge cutoff, the model roster, the availability and fast-mode blurbs. Copying those would fork the text this repo exists to track, so they stay out, and the exact model id is written only when the caller spells `--model`, since Claude Code otherwise resolves it from the account.

Install it ahead of the real launcher on PATH with `install -m 755 shim/claude ~/.local/shims/claude`. It fails open at every step: a missing or unreadable prompt file, a caller that already passes a `--system-prompt*` flag of its own, or a merge that cannot be written all fall back to a plain passthrough.

Settings files are not an alternative route. In 2.1.240, `systemPromptFile` as a string, `systemPromptFile` as `{"type": "file", "path": …}`, and `systemPrompt` as the same object all leave the captured prompt byte-identical to the baseline, with no warning; a `model` key in the same file does take effect, which is what proves the file was read at all.

## Caveats

A capture diff can move for two different reasons: Anthropic changed the prompt, or the runner picked up a new Claude Code version. The provenance header and the per-version archive separate them.

The prompt text here is Anthropic's, reproduced as observed for change tracking.
