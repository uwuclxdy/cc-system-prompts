"""Placeholder normalization: strip per-instance values so captures diff cleanly.

Every rule replaces a value that changes per run with a stable placeholder.
Upstream prompt changes then show as the only diff between two captures.
"""

import os
import re

# transport metadata: the block rides the wire, then vanishes before the model
# sees it (MEASURED 2026-08-27: a native session and a clauth session both
# report the line absent from their context). the artifact of record drops it.
_BILLING_LINE = re.compile(r"^x-anthropic-billing-header:.*(?:\n|$)", re.MULTILINE)

_LINE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(Primary working directory: ).+"), r"\1<cwd>"),
    (re.compile(r"(Is a git repository: )(?:true|false)"), r"\1<git-repo>"),
    # a subagent's <env> block asks the same two questions in its own words.
    # anchored, since the cwd one is otherwise a suffix of the rule above it
    (re.compile(r"^(Working directory: ).+", re.MULTILINE), r"\1<cwd>"),
    (re.compile(r"^(Is directory a git repo: )(?:Yes|No)", re.MULTILINE), r"\1<git-repo>"),
    (re.compile(r"(OS Version: ).+"), r"\1<os-version>"),
    (
        re.compile(r"(You are powered by the model named ).+?(\. The exact model ID is ).+?(\.)"),
        r"\1<model>\2<model-id>\3",
    ),
    (re.compile(r"(Assistant knowledge cutoff is ).+?(\.)"), r"\1<cutoff>\2"),
    # memory dir path embeds a per-run scratch id and the dash-encoded cwd
    (re.compile(r"`[^`]*/memory/`"), "`<memory-dir>`"),
    # quota state rides in the prompt; it varies with account usage
    (
        re.compile(r"<total_tokens>[\d,]+ tokens left</total_tokens>"),
        "<total_tokens><tokens-left> tokens left</total_tokens>",
    ),
    # the gitStatus block names whatever repo the capture's workdir resolved to.
    # captures get published, so branch names, the git identity, working-tree
    # paths and commit subjects all have to go. the labels stay: their wording
    # is upstream prompt text and a change to it is what this repo tracks.
    (re.compile(r"(Current branch: ).+"), r"\1<branch>"),
    (
        re.compile(r"(Main branch \(you will usually use this for PRs\): ).+"),
        r"\1<main-branch>",
    ),
    (re.compile(r"(Git user: ).+"), r"\1<git-user>"),
]

# Bodies rather than single lines, so they need the whole remaining block.
_GIT_STATUS_BODY = re.compile(r"(\nStatus:\n)(?:(?!\nRecent commits:)[\s\S])*")
_GIT_COMMITS_BODY = re.compile(r"(\nRecent commits:\n?)[\s\S]*")

_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def normalize(text: str) -> str:
    text = _BILLING_LINE.sub("", text)
    for pattern, repl in _LINE_RULES:
        text = pattern.sub(repl, text)
    text = _GIT_STATUS_BODY.sub(r"\1<git-status>", text)
    text = _GIT_COMMITS_BODY.sub(r"\1<recent-commits>", text)
    text = _UUID.sub("<session-id>", text)
    text = _ISO_DATE.sub("<date>", text)
    # belt and braces: no home path survives, including ones no line rule matched
    home = os.path.expanduser("~")
    text = text.replace(home, "<home>")
    # some paths spell the home dir dash-encoded (scratch memory dirs)
    text = text.replace(home.lstrip("/").replace("/", "-"), "<home>")
    # last: a bare account name reaches the prompt with no path around it, which
    # every path-shaped rule above misses. `Git user:` was one such field.
    # runs after the path rules so those still see an intact home path.
    return text.replace(os.path.basename(home), "<user>")
