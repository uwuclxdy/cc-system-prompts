# cc-system-prompts

Extracted Claude Code per-model system prompts as reviewable diffs.

Claude Code builds its system prompt on the client, so the prompt a given model receives is observable without a single model request leaving the machine. This repo captures that prompt per model, normalizes it, and commits it. A daily job captures every release that landed since the last run, snapshots each one, and opens a PR when anything moved, which turns a silent upstream prompt change into a diff someone can read. The PR description lists each changed prompt with its added/removed line counts and the changed lines themselves. The release list comes from the Claude Code changelog; the binaries come from the official release CDN.

## What it captures

Two flavors exist, and the CLI picks between them by spawn shape:

| flavor | spawn | identity line | file |
|---|---|---|---|
| cli | interactive pty | `You are Claude Code, Anthropic's official CLI for Claude` | `captures/<model>.md` |
| sdk | `claude -p` | `You are a Claude agent, built on Anthropic's Claude Agent SDK` | `captures/<model>-sdk.md` |

A `claude -p` run marks the session non-interactive, so capturing the CLI flavor needs a real pty rather than a pipe. The capture tool forks one.

## How it works

No model request leaves the machine. A stdlib HTTP server binds a loopback port, the CLI is pointed at it through `ANTHROPIC_BASE_URL` with a dummy key, and the server records each request's `system` blocks and answers 400. One rejected request per model is enough.

The CLI still makes its own background calls around that request, to Anthropic's and GitHub's endpoints, and runs `gh` to look up the user's recently closed Claude Code issues. The capture turns each spawn's self-updater off, resolves the launcher once per run, and refuses a capture whose own `cc_version` stamp names a release other than the one the run records. It gives `gh` no config, no token and no session bus (its keyring), so `gh` runs logged out.

Each spawn gets a fresh config dir seeded with onboarding plus folder trust, and a temporary working directory, so no project's files or history are loaded as context. That is not the same as a context-free prompt: through 2.1.267, Claude Code walks up from the temp dir looking for a git checkout and stamps a `gitStatus:` block into the prompt when it finds one; 2.1.268 dropped that block from the `system` blocks this capture records, and on 2.1.281 it arrives in the first user message (`messages[0]`) instead.

Whether it finds a checkout is a property of the machine, not of the capture, which made that whole block read as drift at every boundary between a laptop and a CI runner. So the temp dir is given its own empty repository, with its own local git identity, and the block's values are then the same on every machine. They are normalized away regardless; the labels are upstream prompt text, and a change to them is exactly what this repo tracks. The seed stays in place for any future version that puts repo-derived content back into the system blocks.

A capture is refused only when it is provably a different artifact than the one asked for: every request carries a billing header in which Claude Code stamps the spawn's own flavor (`cc_entrypoint=cli` for the pty, `sdk-cli` for `-p`), and a stamp naming the other flavor means the runner and the wire disagreed. A missing or renamed stamp cannot discriminate and passes. A prompt whose tracked text moved upstream — a reworded identity line, a dropped block — is a valid capture: it lands as a drift diff, never as a failure.

So normalization, not isolation, is what makes a capture publishable. Before writing, the tool replaces everything machine-specific: dates, working directory, OS version, model line, knowledge cutoff, session ids, remaining-token lines, memory paths, and — in versions that still send one — the whole `gitStatus:` body (branch names, git identity, working-tree paths, commit subjects). The block's labels survive, because their wording is upstream prompt text and a change to it is exactly what this repo tracks.

No home path survives, in either the slash-separated or the dash-encoded spelling. A bare account name is caught separately, since it arrives with no path around it for a path-shaped rule to match.

Every capture set carries a `meta.json` sidecar naming the observation date, the Claude Code version, and the per-file model id. The `.md` files hold prompt text only.

## The subagent prompt

A Task subagent is sent a different system prompt, and that one reaches the wire only when something spawns a subagent. A rejected request never does. So the recorder has a second mode: it answers one request with a streamed `tool_use` for the spawning tool, which makes the CLI spawn a subagent for real. The subagent's own request then lands in the same recorder.

The spawning tool is named `Agent` as of 2.1.241 and was `Task` before it, so the probe reads the name off the request's own tool list. Requests are told apart by Claude Code's own billing header, which stamps `cc_is_subagent=true` on a subagent's.

All four Claude models normalize to a byte-identical subagent prompt, so `captures/subagent.md` covers them together. A non-Claude model is told its name differently and gets no knowledge-cutoff line, which `captures/subagent-deepseek.md` records separately.

Pointing the same probe at the shim answers a second question: whether a session's `--system-prompt-file` reaches its subagents. It does not. A shim'd parent carrying 53 lines of custom prompt spawned a subagent carrying none, and that subagent's prompt matched a stock parent's byte for byte.

## Layout

| path | role |
|---|---|
| `src/cc_prompts/` | the capture tool: recorder, spawn drivers, normalizer, subagent probe |
| `captures/` | current normalized captures, one file per model per flavor, plus the two subagent captures; `meta.json` holds the set's provenance |
| `archive/<cc-version>/` | one snapshot per release, each with its own `meta.json`; adjacent dirs diff to the prompt change |
| `scripts/refresh-captures.sh` | capture every release since the last one and archive each; the same script CI runs |
| `shim/claude` | PATH wrapper that puts a custom system prompt in front of every spawn |

## Running it

```sh
uv sync --frozen
uv run cc-prompts-capture                  # every model, both flavors
uv run cc-prompts-capture --models opus --mode cli
uv run cc-prompts-subagent                 # spawn a subagent, print its prompt's size
./scripts/refresh-captures.sh              # capture every release since the last committed capture
```

`refresh-captures.sh` downloads each release binary from the official CDN, checksum-verified against the release manifest, so nothing on the machine decides which version gets recorded. A manual `cc-prompts-capture` still runs against the real `claude` launcher: pointing it at a wrapper that injects `--system-prompt-file` would record that custom prompt instead of the stock one, so the capture is refused when its text matches the custom prompt's.

## The shim

`shim/claude` is a PATH wrapper that hands the real launcher a `--system-prompt-file`. That flag replaces only the third entry of the stock system array — the billing header and the identity line survive — and leaves the session-facts message intact in both the pty and the `-p` flavor. A session still knows its working directory, platform, and OS version, because those facts arrive in that message (`messages[1]`, a role `system` message outside the system array) rather than in the entry the flag replaces. In `-p` the flag also drops the stock gitStatus reminder from the first user message; the pty flavor keeps it. The shim passes `~/.claude/system-prompt/cc-sys.md` itself to the flag.

Install it ahead of the real launcher on PATH with `install -m 755 shim/claude ~/.local/shims/claude`. It fails open at every step: a missing or unreadable prompt file, or a caller that already passes a `--system-prompt*` or `--append-system-prompt*` flag of its own, falls back to a plain passthrough.

Settings files are not an alternative route. In 2.1.240, `systemPromptFile` as a string, `systemPromptFile` as `{"type": "file", "path": …}`, and `systemPrompt` as the same object all leave the captured prompt byte-identical to the baseline, with no warning; a `model` key in the same file does take effect, which is what proves the file was read at all.

## Caveats

A capture diff can move for two different reasons: Anthropic changed the prompt, or a release landed. The provenance sidecar and the per-version archive separate them; adjacent archive dirs differ only in `meta.json` when a release changed no prompt bytes.

The prompt text here is Anthropic's, reproduced as observed for change tracking.
