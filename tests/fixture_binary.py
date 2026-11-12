"""Synthetic `// @bun @bytecode` release binaries for the strings tests.

Never a real release: every module is hand-written JS whose kept strings and
rule tags are hand-derived from the extractor's keep rules; the pinned row keys
are the first 16 hex chars of the row text's sha256.
"""

import hashlib
from pathlib import Path

MARKER = b"// @bun @bytecode"

PROSE = (
    "When you edit a file, do not reformat unrelated lines; keep the diff "
    "minimal and explain why the change matters."
)
CONFIRM_PROSE = (
    "If the user asks for deletion, always confirm the target first and never "
    "proceed without an explicit confirmation from them."
)
REPLACED_TEXT = "Run a shell command"
REPLACEMENT_TEXT = "Run a shell command now."

MODULE_SOURCES = [
    # exporter: BN="Bash" exported as BASH_NAME, resolved inside module 1's template
    'var BN="Bash";function f(){return BN}export{BN as BASH_NAME};',
    # resolved and unresolved interpolations, both description-anchored
    (
        'import{BASH_NAME as BN2}from"./m0.js";'
        "var o={description:`Use the ${BN2} tool to edit files.`};"
        "var e={description:`Use the ${mysteryVar} tool to edit files.`};"
    ),
    # description + searchHint property values
    (
        'var d={description:"Use the Bash tool to run shell commands.",'
        'searchHint:"Run a shell command"};'
    ),
    # reminder template with an unresolved interpolation
    "var t=`Before replies, check the <system-reminder> state. ${v}`;",
    # prose twice -> occurs 2
    f'var a="{PROSE}";var b="{PROSE}";',
    # hook, singular so HOOK_RE's \\bhook\\b matches
    'var h="Register a git hook that formats staged files.";',
    # array holding a kept description member + a 24+ char sibling
    (
        'var arr=[{description:"Short tool help text"},'
        '"a sibling string at least twenty-four characters"];'
    ),
    # ternary: branch 1 kept by prose (a description value would sit behind the
    # prop colon, where ctx[-1] is ":" and the ?/: pair detector never fires)
    (f'var t2=flag?"{CONFIRM_PROSE}":"branch b sibling text at least sixteen chars";'),
    # bunfs embedded-FS import path, dropped outright
    'var m=import*as ns from"/$bunfs/root/mod-1.js";',
]


def _row(key: str, text: str, rules: list[str], occurs: int) -> dict:
    return {"key": key, "text": text, "rules": rules, "occurs": occurs}


# rows sorted by text, exactly what `extract` returns for the base fixture
EXPECTED_ROWS = [
    _row(
        "59bd7fcebb3c9015",
        "Before replies, check the <system-reminder> state. {placeholder}",
        ["reminder"],
        1,
    ),
    _row("7acb7a6678e0b9b4", CONFIRM_PROSE, ["prose", "ternary-sibling"], 1),
    _row("212baf0ee0d5822a", "Register a git hook that formats staged files.", ["hook"], 1),
    _row("5aab063b5604050c", "Run a shell command", ["description"], 1),
    _row("f92c1f9e304032a4", "Short tool help text", ["description"], 1),
    _row("ded3b1f1ad6226de", "Use the Bash tool to edit files.", ["description"], 1),
    _row("f008d98d69d8add4", "Use the Bash tool to run shell commands.", ["description"], 1),
    _row("470533db32304ec3", "Use the {placeholder} tool to edit files.", ["description"], 1),
    _row("0cc88e6078e7b5a1", PROSE, ["prose"], 2),
    _row(
        "09c2959afa634019",
        "a sibling string at least twenty-four characters",
        ["array-sibling"],
        1,
    ),
    _row(
        "b71b8c52addc77d4",
        "branch b sibling text at least sixteen chars",
        ["ternary-sibling"],
        1,
    ),
]


def _pad(source: str) -> str:
    # carve skips segments under 200 bytes, so every module pads past the floor
    return source + ";var pad=1;" * max(0, (200 - len(source)) // 11 + 1)


def build_binary(
    dest: Path,
    *,
    reword_search_hint: bool = False,
    renamed_identifier: bool = False,
    extra_head_module: bool = False,
) -> Path:
    """Write a synthetic release binary and return its path."""
    sources = list(MODULE_SOURCES)
    if reword_search_hint:
        sources = [s.replace(REPLACED_TEXT, REPLACEMENT_TEXT) for s in sources]
    if renamed_identifier:
        sources = [s.replace("mysteryVar", "mysteryVar2") for s in sources]
    modules = [MARKER + _pad(s).encode() + b"\x00" for s in sources]
    if extra_head_module:
        modules.insert(0, MARKER + _pad(";var z=1;" * 10).encode() + b"\x00")
    dest.write_bytes(b"JUNK-HEADER" + b"".join(modules) + b"JUNK-TRAILER")
    return dest


def row_text_key(text: str) -> str:
    """The stable row key: the first 16 hex chars of the text's sha256."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
