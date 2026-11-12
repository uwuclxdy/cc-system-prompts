"""Extract model-facing strings from a Claude Code release binary.

Carves every `// @bun @bytecode` segment of at least 200 bytes from the
binary, tokenizes each module (string and template literals, skipping comments
and regex literals), keeps the literals matching the rule set in
`is_model_facing`, and normalizes `${...}` interpolations to `{placeholder}`
so bundler renames and module offsets cannot move a row. One row per distinct
text, sorted by text, so per-release JSONL files diff readably.
"""

import hashlib
import json
import mmap
import re
from collections.abc import Iterator
from pathlib import Path

MARKER = b"// @bun @bytecode"
MIN_MODULE_BYTES = 200

DEFAULT_MIN_CHARS = 80
DEFAULT_MIN_WORDS = 12

# instruction-prose idioms: second person and directive markers
IDIOM_RE = re.compile(
    r"\byou\b|\byour\b|\bdo not\b|\bdon.t\b|\bnever\b"
    r"|\balways\b|\bmust\b|\bshould\b|\bavoid\b"
    r"|\buse this\b|\bthis tool\b|\binstead\b"
    r"|\bif the user\b|\bthe user\b|\bcan use\b|\bbetter to\b|\bwhen you\b",
    re.IGNORECASE,
)
CAPS_RE = re.compile(r"IMPORTANT|CRITICAL|REMINDER")
HOOK_RE = re.compile(r"(?i)\bhook\b")
REMINDER_RE = re.compile(r"<system-reminder>")
ANCHOR_PROPS = {"description", "searchHint", "prompt"}
PLACEHOLDER = "{placeholder}"

WS = " \t\r\n"
IDENT_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
NUM_RE = re.compile(r"0[xXbBoO][0-9a-fA-F_]+n?|[0-9]+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?n?")
REGEX_KEYWORDS = {
    "return",
    "typeof",
    "instanceof",
    "in",
    "of",
    "new",
    "delete",
    "void",
    "throw",
    "case",
    "do",
    "else",
    "yield",
    "await",
}
SIMPLE_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "0": "\0",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "`": "`",
    "$": "$",
    "\n": "",
}


def extract(binary: Path) -> list[dict]:
    """Every model-facing string in the binary, one row per distinct text.

    Rows are {"key", "text", "rules" (sorted list), "occurs"}, sorted by text;
    raises RuntimeError when the binary holds no bundle segments, or when its
    modules keep no strings at all.
    """
    mods = carve(binary)
    if not mods:
        raise RuntimeError(f"carved no modules from {binary}")
    resolver = Resolver(mods)
    rows: dict[str, dict] = {}
    for idx, (_, raw) in enumerate(mods):
        tz = Tokenizer(raw.decode("utf-8", "replace"), resolver.dict_for(idx))
        lits = list(tz.literals())
        flags = [
            is_model_facing(text, ctx, DEFAULT_MIN_CHARS, DEFAULT_MIN_WORDS)
            for text, ctx, _, _ in lits
        ]
        final = co_anchors(tz.bracket_pairs(), lits, flags)
        for (text, _, _, _), rules in zip(lits, final, strict=True):
            if not rules:
                continue
            row = rows.get(text)
            if row is None:
                rows[text] = {
                    "key": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
                    "text": text,
                    "rules": sorted(set(rules)),
                    "occurs": 1,
                }
            else:
                # a repeated literal keeps the rules of its first occurrence
                row["occurs"] += 1
    if not rows:
        raise RuntimeError(f"kept no strings from {binary}")
    return sorted(rows.values(), key=lambda row: row["text"])


def render_jsonl(rows: list[dict]) -> str:
    """One JSON line per row, `rules` comma-joined, lines in text order."""
    lines = [
        json.dumps({**row, "rules": ",".join(row["rules"])}, ensure_ascii=False, sort_keys=True)
        for row in sorted(rows, key=lambda row: row["text"])
    ]
    return "".join(line + "\n" for line in lines)


def carve(binary_path: Path) -> list[tuple[int, bytes]]:
    """Every @bun @bytecode segment >= MIN_MODULE_BYTES as (offset, bytes), binary order."""
    if binary_path.stat().st_size == 0:
        # mmap cannot map an empty file, and an empty binary carves nothing anyway
        return []
    with open(binary_path, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        size = len(mm)
        mods = []
        off = mm.find(MARKER)
        while off != -1:
            src_off = off + len(MARKER)
            nul = mm.find(b"\x00", src_off)
            end = nul if nul != -1 else size
            if end - src_off >= MIN_MODULE_BYTES:
                mods.append((off, mm[src_off:end]))
            off = mm.find(MARKER, off + 1)
        mm.close()
    return mods


SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
IMPORT_RE = re.compile(r"import\{([^{}]*)\}from\"([^\"]*)\"")
EXPORT_TAIL_RE = re.compile(r"export\{([^{}]*)\}")
ASSIGN_RE = re.compile(r"(?<![A-Za-z0-9_$])([A-Za-z_$][A-Za-z0-9_$]*)=(?![=>])[\"`]")


def parse_pairs(blob: str) -> list[tuple[str, str]]:
    """`a as b, c` -> [(exported, local), ...] for import/export tails."""
    pairs = []
    for part in blob.split(","):
        part = part.strip()
        if not part:
            continue
        if " as " in part:
            a, b = part.split(" as ", 1)
            pairs.append((a.strip(), b.strip()))
        else:
            pairs.append((part, part))
    return pairs


class Resolver:
    """Cross-module constant resolver: ${Ie} in a module resolves through that
    module's import map to the exporting module's string-literal value. Only
    exported symbols resolve (top-level by construction, no shadowing). An
    exported name whose candidate definitions disagree stays unresolved."""

    def __init__(self, mods: list[tuple[int, bytes]]) -> None:
        self.mods = mods
        self.imports: list[dict[str, str]] = [dict() for _ in mods]  # idx: local -> exported name
        self.export_candidates: dict[str, list[tuple[int, str]]] = {}  # name -> [(idx, local)]
        self.own_export_locals: list[set[str]] = [set() for _ in mods]
        self.values: list[dict[str, str]] = [dict() for _ in mods]  # idx: local -> decoded value
        self.dicts: list[dict[str, str] | None] = [None] * len(mods)
        for idx, (_, raw) in enumerate(mods):
            t = raw.decode("utf-8", "replace")
            for m in IMPORT_RE.finditer(t):
                for exported, local in parse_pairs(m.group(1)):
                    self.imports[idx][local] = exported
            for m in EXPORT_TAIL_RE.finditer(t):
                for local, exported in parse_pairs(m.group(1)):
                    self.export_candidates.setdefault(exported, []).append((idx, local))
                    self.own_export_locals[idx].add(local)
            for m in ASSIGN_RE.finditer(t):
                sym = m.group(1)
                if sym not in self.own_export_locals[idx] or sym in self.values[idx]:
                    continue
                dec, _ = Tokenizer(t).scan_string(m.end() - 1)
                if dec is not None:
                    self.values[idx][sym] = combine_surrogates(dec)

    def dict_for(self, idx: int) -> dict[str, str]:
        d = self.dicts[idx]
        if d is None:
            d = {}
            for local, exported in self.imports[idx].items():
                vals = set()
                for idx2, local2 in self.export_candidates.get(exported, []):
                    v = self.values[idx2].get(local2)
                    if v is not None:
                        vals.add(v)
                if len(vals) == 1:
                    d[local] = vals.pop()
            for local in self.own_export_locals[idx]:
                v = self.values[idx].get(local)
                if v is not None and local not in d:
                    d[local] = v
            self.dicts[idx] = d
        return d


def combine_surrogates(text: str) -> str:
    """Join UTF-16 surrogate pairs; a leftover lone surrogate (invalid either way)
    becomes U+FFFD so every output row is valid UTF-8."""
    if not SURROGATE_RE.search(text):
        return text
    out = []
    k = 0
    while k < len(text):
        ch = text[k]
        if "\ud800" <= ch <= "\udbff" and k + 1 < len(text) and "\udc00" <= text[k + 1] <= "\udfff":
            high, low = ord(ch), ord(text[k + 1])
            out.append(chr(0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00)))
            k += 2
        elif "\ud800" <= ch <= "\udfff":
            out.append("�")
            k += 1
        else:
            out.append(ch)
            k += 1
    return "".join(out)


class Tokenizer:
    """Yields (text, pre_context_tokens) for every string/template literal in one
    JS module, skipping comments and regex literals, decoding escapes, turning
    ${...} interpolation into PLACEHOLDER, and joining +-chains of literals."""

    def __init__(self, src: str, resolve_dict: dict[str, str] | None = None) -> None:
        self.s = src
        self.n = len(src)
        self.resolve_dict = resolve_dict or {}
        self.toks: list[str] = []
        self.last_kind = None  # 'ident' | 'punct' | 'str' | 'num' | 'regex' | None

    def push_tok(self, tok: str, kind: str) -> None:
        self.toks.append(tok)
        if len(self.toks) > 12:
            self.toks.pop(0)
        self.last_kind = kind

    def regex_starts_here(self) -> bool:
        if self.last_kind is None:
            return True
        if self.last_kind == "ident":
            return self.toks[-1] in REGEX_KEYWORDS
        if self.last_kind in ("str", "num", "regex"):
            return False
        return not (self.toks and self.toks[-1] in (")", "]", "}"))

    def decode_escape(self, j: int) -> tuple[str, int]:
        """j is at the backslash. Return (decoded, index_after_escape)."""
        s, n = self.s, self.n
        k = j + 1
        if k >= n:
            return "\\", k
        c = s[k]
        if c in SIMPLE_ESCAPES:
            return SIMPLE_ESCAPES[c], k + 1
        if c == "x":
            m = re.match(r"[0-9a-fA-F]{2}", s[k + 1 : k + 3])
            if m:
                return chr(int(m.group(0), 16)), k + 3
            return "x", k + 1
        if c == "u":
            if s[k + 1 : k + 2] == "{":
                m = re.match(r"[0-9a-fA-F]{1,6}", s[k + 2 : k + 8])
                if m:
                    return chr(int(m.group(0), 16)), k + 2 + len(m.group(0)) + 1
                return "u", k + 1
            m = re.match(r"[0-9a-fA-F]{4}", s[k + 1 : k + 5])
            if m:
                return chr(int(m.group(0), 16)), k + 5
            return "u", k + 1
        return c, k + 1

    def skip_string(self, j: int, quote: str) -> int:
        """Opaque skip (no decode) used inside interpolation args."""
        s, n = self.s, self.n
        j += 1
        while j < n:
            c = s[j]
            if c == "\\":
                j += 2
                continue
            if c == quote or c == "\n":
                return j + 1
            j += 1
        return n

    def skip_template(self, j: int) -> int:
        s, n = self.s, self.n
        j += 1
        while j < n:
            c = s[j]
            if c == "\\":
                j += 2
                continue
            if c == "`":
                return j + 1
            if c == "$" and s[j + 1 : j + 2] == "{":
                depth = 1
                j += 2
                while j < n and depth:
                    c2 = s[j]
                    if c2 == "{":
                        depth += 1
                    elif c2 == "}":
                        depth -= 1
                    elif c2 in "'\"":
                        j = self.skip_string(j, c2)
                        continue
                    elif c2 == "`":
                        j = self.skip_template(j)
                        continue
                    elif (
                        c2 == "/"
                        and s[j + 1 : j + 2] not in ("", "/", "*")
                        and self.arg_regex_here(s, j)
                    ):
                        j = self.skip_regex(j)
                        continue
                    j += 1
                continue
            j += 1
        return n

    def skip_regex(self, j: int) -> int:
        s, n = self.s, self.n
        j += 1
        in_class = False
        while j < n:
            c = s[j]
            if c == "\\":
                j += 2
                continue
            if c == "\n":
                return j
            if c == "[":
                in_class = True
            elif c == "]":
                in_class = False
            elif c == "/" and not in_class:
                j += 1
                while j < n and s[j].isalpha():
                    j += 1
                return j
            j += 1
        return n

    def skip_comment(self, j: int) -> int:
        s, n = self.s, self.n
        if s.startswith("//", j):
            k = s.find("\n", j)
            return (k + 1) if k != -1 else n
        k = s.find("*/", j + 2)
        return (k + 2) if k != -1 else n

    def arg_regex_here(self, s: str, j: int) -> bool:
        prev = s[j - 1] if j > 0 else ""
        return not (prev in ")]}" or prev.isalnum() or prev in "_$")

    def decode_template_arg(self, k: int) -> tuple[str | None, int]:
        """k is just past '${'. Return (text_or_None, index_after_closing_brace).
        text is the flattened constant when the argument is a pure literal
        concatenation; None means the argument holds a variable expression and
        the caller emits PLACEHOLDER."""
        s, n = self.s, self.n
        toks: list[tuple] = []  # ('lit', start, end) | ('plus',) | ('other',)
        depth = 1
        j = k
        closed_at = None
        while j < n:
            c = s[j]
            if c == "}":
                depth -= 1
                if depth == 0:
                    closed_at = j
                    break
                toks.append(("other",))
                j += 1
                continue
            if c == "{":
                depth += 1
                toks.append(("other",))
                j += 1
                continue
            if c in "'\"`":
                e = self.skip_template(j) if c == "`" else self.skip_string(j, c)
                toks.append(("lit", j, e))
                j = e
                continue
            if c == "/" and s[j + 1 : j + 2] == "/":
                nl = s.find("\n", j)
                if nl == -1:
                    return None, n
                j = nl
                continue
            if c == "/" and s[j + 1 : j + 2] == "*":
                close = s.find("*/", j + 2)
                j = (close + 2) if close != -1 else n
                continue
            if c == "/" and self.arg_regex_here(s, j):
                toks.append(("other",))
                j = self.skip_regex(j)
                continue
            if c == "+":
                toks.append(("plus",))
                j += 1
                continue
            if c in WS:
                j += 1
                continue
            m = IDENT_RE.match(s, j)
            if m:
                toks.append(("ident", m.group(0)))
                j = m.end()
                continue
            toks.append(("other",))
            j += 1
        if closed_at is None:
            return None, n
        end = closed_at + 1
        kinds = [t[0] for t in toks]
        if kinds and kinds == ["ident"]:
            v = self.resolve_dict.get(toks[0][1])
            return (v, end) if v is not None else (None, end)
        if not kinds or "other" in kinds or "lit" not in kinds:
            return None, end
        if (
            kinds[0] == "plus"
            or kinds[-1] == "plus"
            or any(a == "plus" and b == "plus" for a, b in zip(kinds, kinds[1:], strict=False))
        ):
            return None, end
        pieces = []
        for t in toks:
            if t[0] == "lit":
                dec, _ = self.scan_string(t[1])
                if dec is None:
                    return None, end
                pieces.append(dec)
            elif t[0] == "ident":
                v = self.resolve_dict.get(t[1])
                if v is None:
                    return None, end
                pieces.append(v)
            else:
                pieces.append("")
        return "".join(pieces), end

    def scan_string(self, j: int) -> tuple[str | None, int]:
        """Decode a literal starting at index j (a quote char). Returns
        (decoded_text_or_None, end_index). Template interpolation -> PLACEHOLDER."""
        s, n = self.s, self.n
        quote = s[j]
        k = j + 1
        out = []
        while k < n:
            c = s[k]
            if c == "\\":
                dec, k = self.decode_escape(k)
                out.append(dec)
                continue
            if quote == "`":
                if c == "`":
                    return "".join(out), k + 1
                if c == "$" and s[k + 1 : k + 2] == "{":
                    text, k2 = self.decode_template_arg(k + 2)
                    if text is None:
                        out.append(PLACEHOLDER)
                    else:
                        out.append(text)
                    k = k2
                    continue
                out.append(c)
                k += 1
            else:
                if c == "\n":
                    return None, k
                if c == quote:
                    return "".join(out), k + 1
                out.append(c)
                k += 1
        return None, n

    def chain_operand(self, end: int) -> tuple[str, int] | None:
        """After a literal: '+ <literal>' or '+ <identifier>' -> (piece, next_end),
        else None. Identifier operands become PLACEHOLDER and are consumed."""
        s, n = self.s, self.n
        k = end
        while k < n and s[k] in WS:
            k += 1
        if k >= n or s[k] != "+":
            return None
        k += 1
        while k < n and s[k] in WS:
            k += 1
        if k >= n:
            return None
        c = s[k]
        if c in "'\"`":
            dec, e = self.scan_string(k)
            if dec is None:
                return None
            return (dec, e)
        m = IDENT_RE.match(s, k)
        if m:
            v = self.resolve_dict.get(m.group(0))
            if v is not None:
                return (v, m.end())
            return (PLACEHOLDER, m.end())
        return None

    def literals(self) -> Iterator[tuple[str, list[str], int, int]]:
        """Yields (text, ctx, start, end): decoded text, token context before it,
        and the source span of the whole +-chain."""
        s, n = self.s, self.n
        j = 0
        while j < n:
            c = s[j]
            if c in WS:
                j += 1
                continue
            if c == "/" and s[j + 1 : j + 2] == "/":
                j = self.skip_comment(j)
                continue
            if c == "/" and s[j + 1 : j + 2] == "*":
                j = self.skip_comment(j)
                continue
            if c in "'\"`":
                pre_ctx = list(self.toks)
                start = j
                dec, e = self.scan_string(j)
                self.push_tok("STR", "str")
                if dec:
                    text = combine_surrogates(dec)
                    yield text, pre_ctx, start, e
                    while True:
                        op = self.chain_operand(e)
                        if op is None:
                            break
                        piece, e = op
                        text = combine_surrogates(text + piece)
                        yield text, pre_ctx, start, e
                    j = e
                    continue
                j = e if e > j else j + 1
                continue
            if c == "/" and self.regex_starts_here():
                j = self.skip_regex(j)
                self.push_tok("RE", "regex")
                continue
            if c.isdigit():
                m = NUM_RE.match(s, j)
                j = m.end() if m else j + 1
                self.push_tok("NUM", "num")
                continue
            m = IDENT_RE.match(s, j)
            if m:
                self.push_tok(m.group(0), "ident")
                j = m.end()
                continue
            self.push_tok(c, "punct")
            j += 1

    def bracket_pairs(self) -> list[tuple[int, int]]:
        """(open, close) spans for every [ ] pair, string/comment/regex aware."""
        s, n = self.s, self.n
        j = 0
        stack = []
        pairs = []
        prev = ""  # last significant char: regex-vs-division heuristic
        while j < n:
            c = s[j]
            if c in "'\"`":
                _, e = self.scan_string(j)
                prev = c
                j = e if e > j else j + 1
                continue
            if c == "/" and s[j + 1 : j + 2] == "/":
                j = self.skip_comment(j)
                prev = "\n"
                continue
            if c == "/" and s[j + 1 : j + 2] == "*":
                j = self.skip_comment(j)
                continue
            if c == "/" and not (prev in ")]}" or prev.isalnum() or prev in "_$"):
                j = self.skip_regex(j)
                prev = "/"
                continue
            if c == "[":
                stack.append(j)
                prev = c
                j += 1
                continue
            if c == "]":
                if stack:
                    pairs.append((stack.pop(), j))
                prev = c
                j += 1
                continue
            if c in WS:
                j += 1
                continue
            prev = c
            j += 1
        return pairs


def co_anchors(
    pairs: list[tuple[int, int]],
    lits: list[tuple[str, list[str], int, int]],
    flags: list[list[str]],
) -> list[list[str]]:
    """Array-sibling and ternary-sibling co-anchoring: a short literal joins its
    kept neighbours. lits: [(text, ctx, start, end)]; flags: base rule lists."""
    n = len(lits)
    final = [list(f) for f in flags]
    # innermost containing [ ] per literal via a sweep over bracket events
    events = []
    for pi, (p, q) in enumerate(pairs):
        events.append((p, 0, pi))
        events.append((q, 2, pi))
    order = sorted(range(n), key=lambda i: lits[i][2])
    events.sort()
    ei = 0
    stack = []
    container: list[int | None] = [None] * n
    members: dict[int, list[int]] = {}
    for oi in order:
        a = lits[oi][2]
        while ei < len(events) and events[ei][0] < a:
            pos, kind, pi = events[ei]
            if kind == 0:
                stack.append(pi)
            else:
                if stack and stack[-1] == pi:
                    stack.pop()
                elif pi in stack:
                    stack.remove(pi)
            ei += 1
        container[oi] = stack[-1] if stack else None
    for i, c in enumerate(container):
        if c is not None:
            members.setdefault(c, []).append(i)
    for _c, mem in members.items():
        if any(final[i] for i in mem):
            for i in mem:
                if len(lits[i][0]) >= 24 and "array-sibling" not in final[i]:
                    final[i].append("array-sibling")
    # ternary siblings: consecutive literals preceded by '?' then ':'
    for i in range(n - 1):
        t1, ctx1, a1, _ = lits[i]
        t2, ctx2, a2, b2 = lits[i + 1]
        if not (ctx1 and ctx1[-1] == "?" and ctx2 and ctx2[-1] == ":"):
            continue
        if b2 - a1 > 4000:
            continue
        if (final[i] and len(t2) >= 16) or (final[i + 1] and len(t1) >= 16):
            if len(t2) >= 16 and "ternary-sibling" not in final[i + 1]:
                final[i + 1].append("ternary-sibling")
            if len(t1) >= 16 and "ternary-sibling" not in final[i]:
                final[i].append("ternary-sibling")
    return final


def is_model_facing(text: str, ctx: list[str], min_chars: int, min_words: int) -> list[str]:
    """The stated rule set. Returns the fired rule names (empty = drop).

    Bun embedded-FS import paths (/$bunfs/root/...) are dropped outright.
    ctx is the significant-token window immediately before the literal.
    - description: the literal is the value of a description/searchHint/prompt property,
      or returned from a description()/prompt() method
    - reminder: the text itself contains <system-reminder>
    - prose: length/word floor plus an instruction-prose idiom
    - hook: any string of >= 12 chars mentioning a hook
    """
    rules = []
    if text.startswith("/$bunfs/root/"):
        return []
    if REMINDER_RE.search(text):
        rules.append("reminder")
    if ctx and (
        ctx[-1] in ANCHOR_PROPS or (ctx[-1] == ":" and len(ctx) >= 2 and ctx[-2] in ANCHOR_PROPS)
    ):
        rules.append("description")
    if len(ctx) >= 5 and ctx[-5] in ANCHOR_PROPS and ctx[-4:] == ["(", ")", "{", "return"]:
        rules.append("description")
    if (
        len(text) >= min_chars
        and len(text.split()) >= min_words
        and (IDIOM_RE.search(text) or CAPS_RE.search(text))
    ):
        rules.append("prose")
    if len(text) >= 12 and HOOK_RE.search(text):
        rules.append("hook")
    return rules
