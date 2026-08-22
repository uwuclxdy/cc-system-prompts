"""Placeholder normalization: strip per-instance values so captures diff cleanly.

Every rule replaces a value that changes per run with a stable placeholder.
Upstream prompt changes then show as the only diff between two captures.
"""

import os
import re

_LINE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(Primary working directory: ).+"), r"\1<cwd>"),
    (re.compile(r"(Is a git repository: )(?:true|false)"), r"\1<git-repo>"),
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
]

_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def normalize(text: str) -> str:
    for pattern, repl in _LINE_RULES:
        text = pattern.sub(repl, text)
    text = _UUID.sub("<session-id>", text)
    text = _ISO_DATE.sub("<date>", text)
    # belt and braces: no home path survives, including ones no line rule matched
    text = text.replace(os.path.expanduser("~"), "<home>")
    # some paths spell the home dir dash-encoded (scratch memory dirs)
    encoded_home = os.path.expanduser("~").lstrip("/").replace("/", "-")
    return text.replace(encoded_home, "<home>")
