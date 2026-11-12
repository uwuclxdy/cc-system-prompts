"""Re-capture every CC release since the last committed capture.

`captures/` always holds the newest set, and every release gets its own
`archive/<version>/` snapshot, so adjacent archive dirs diff to a readable
prompt change. Each snapshot also carries `strings.jsonl`, the model-facing
strings extracted from that release's binary, stored only when they differ
from the newest stored set. The version list is the public changelog; the
watermark is the `version` field of `captures/meta.json`. A run with no new
releases re-captures the watermark version and rewrites only what drifted.
"""

import argparse
import difflib
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

from .capture import main as capture_main
from .install import DEFAULT_BASE_URL, install_binary
from .meta import META_NAME, parse_watermark
from .strings_extract import extract, render_jsonl
from .subagent import main as subagent_main
from .versions import CHANGELOG_URL, fetch_changelog, version_key, versions_since

REPORT_PATH = "drift-report.md"
# GitHub caps a PR body at 65536 chars; these budgets keep the body under it
MAX_DIFF_CHARS = 45_000
STRINGS_MAX_CHARS = 15_000
MAX_REPORT_CHARS = 60_000
# ratio pairing is quadratic in the unmatched rows; past this many candidate
# pairs the matcher constructions alone outlast the run (~2.6 minutes at the
# measured 12.7K pairs/s), so pairing skips and the unmatched rows stay plain
# additions and removals
PAIRING_MAX_PAIRS = 2_000_000
# a reworded string keeps most of its characters; below this a pair reads as
# two different strings
STRING_PAIR_RATIO = 0.85
# archive dirs are release snapshots; the shape check keeps a stray dir from
# pairing as one
VERSION_DIR_RE = re.compile(r"^\d+\.\d+\.\d+$")
# the table shows every class; only description/reminder changes inline
INLINE_RULES = ("description", "reminder")
RULE_CLASSES = ("description", "reminder", "prose", "hook", "array-sibling", "ternary-sibling")


def capture_set(binary: str, out: Path) -> None:
    """The whole artifact set for one release: every model, both flavors, both
    subagent probes."""
    out.mkdir(parents=True, exist_ok=True)
    steps = (
        ("cc-prompts-capture", capture_main(["--claude-bin", binary, "--out", str(out)])),
        (
            "cc-prompts-subagent",
            subagent_main(["--claude-bin", binary, "--out", str(out / "subagent.md")]),
        ),
        (
            "cc-prompts-subagent (deepseek)",
            subagent_main(
                [
                    "--claude-bin",
                    binary,
                    "--model",
                    "deepseek-chat",
                    "--out",
                    str(out / "subagent-deepseek.md"),
                ]
            ),
        ),
    )
    for step, exit_code in steps:
        if exit_code != 0:
            raise RuntimeError(f"{step} failed with exit code {exit_code}")


def changed_diffs(prev: Path, cur: Path) -> dict[str, tuple[list[str], int, int]]:
    """Prompt diffs per file: (changed lines with `+`/`-` prefixes, added, removed)."""
    diffs: dict[str, tuple[list[str], int, int]] = {}
    for path in sorted(cur.glob("*.md")):
        cur_lines = path.read_text().splitlines()
        other = prev / path.name
        if not other.exists():
            diffs[path.name] = ([f"+{line}" for line in cur_lines], len(cur_lines), 0)
            continue
        prev_lines = other.read_text().splitlines()
        if prev_lines == cur_lines:
            continue
        lines: list[str] = []
        added = removed = 0
        # autojunk off: prompts repeat short lines (blanks, fences), which the
        # junk heuristic would drop from the diff
        matcher = difflib.SequenceMatcher(a=prev_lines, b=cur_lines, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag in ("delete", "replace"):
                lines += [f"-{line}" for line in prev_lines[i1:i2]]
                removed += i2 - i1
            if tag in ("insert", "replace"):
                lines += [f"+{line}" for line in cur_lines[j1:j2]]
                added += j2 - j1
        diffs[path.name] = (lines, added, removed)
    return diffs


def _by_text(row: dict) -> str:
    return row["text"]


def strings_diff(prev_rows: list[dict] | None, cur_rows: list[dict]) -> dict[str, list] | None:
    """Added and removed rows plus (old, new) tuples one reword apart.

    Rows first match by text: identical rules and occurs drop out (an unchanged
    string is no change), a differing rules or occurs becomes a metadata-only
    pair. Only the unmatched remainder enters ratio pairing, best ratio first
    at STRING_PAIR_RATIO, each row used once; unpaired rows are plain
    leftovers: added from cur, removed from prev, each list sorted by text.
    Past PAIRING_MAX_PAIRS candidate pairs the ratio stage skips entirely and
    the unmatched rows all stay plain additions and removals. None without a
    baseline to diff against.
    """
    if prev_rows is None:
        return None
    prev_by_text = {row["text"]: row for row in prev_rows}
    cur_texts = {row["text"] for row in cur_rows}
    metadata: list[tuple[dict, dict]] = []
    for new in cur_rows:
        old = prev_by_text.get(new["text"])
        if old is not None and (old["rules"] != new["rules"] or old["occurs"] != new["occurs"]):
            metadata.append((old, new))
    old_unmatched = [row for row in prev_rows if row["text"] not in cur_texts]
    new_unmatched = [row for row in cur_rows if row["text"] not in prev_by_text]
    pairing_skipped = len(old_unmatched) * len(new_unmatched) > PAIRING_MAX_PAIRS
    candidates: list[tuple[float, str, str, int, int]] = []
    if not pairing_skipped:
        for i, old in enumerate(old_unmatched):
            for j, new in enumerate(new_unmatched):
                a, b = old["text"], new["text"]
                # the length bound is an O(1) ceiling on the ratio: past it no
                # SequenceMatcher can reach STRING_PAIR_RATIO
                if 2 * min(len(a), len(b)) / (len(a) + len(b)) < STRING_PAIR_RATIO:
                    continue
                matcher = difflib.SequenceMatcher(None, a, b)
                if matcher.quick_ratio() < STRING_PAIR_RATIO:
                    continue
                ratio = matcher.ratio()
                if ratio < STRING_PAIR_RATIO:
                    continue
                candidates.append((ratio, a, b, i, j))
    reworded: list[tuple[dict, dict]] = []
    used_prev: set[int] = set()
    used_cur: set[int] = set()
    for _, _, _, i, j in sorted(candidates, key=lambda cand: (-cand[0], cand[1], cand[2])):
        if i in used_prev or j in used_cur:
            continue
        used_prev.add(i)
        used_cur.add(j)
        reworded.append((old_unmatched[i], new_unmatched[j]))
    added = [new_unmatched[j] for j in range(len(new_unmatched)) if j not in used_cur]
    removed = [old_unmatched[i] for i in range(len(old_unmatched)) if i not in used_prev]
    return {
        "added": sorted(added, key=_by_text),
        "removed": sorted(removed, key=_by_text),
        "changed": sorted(metadata + reworded, key=lambda pair: pair[0]["text"]),
        "pairing_skipped": pairing_skipped,
    }


def _render_changes(version: str, diffs: dict[str, tuple[list[str], int, int]]) -> list[str]:
    """One release's section: a +/- table plus changed-line diff blocks."""
    if not diffs:
        return [f"### {version}", "", "no prompt changes."]
    lines = [f"### {version}", "", "| prompt | +/- |", "| --- | --- |"]
    for name, (_, added, removed) in diffs.items():
        lines.append(f"| `{name}` | +{added} / -{removed} |")
    budget = MAX_DIFF_CHARS
    omitted = 0
    for name, (diff_lines, _, _) in diffs.items():
        if budget <= 0:
            omitted += len(diff_lines)
            continue
        chunk: list[str] = []
        for line in diff_lines:
            cost = len(line) + 1
            if cost > budget:
                break
            chunk.append(line)
            budget -= cost
        omitted += len(diff_lines) - len(chunk)
        lines += ["", f"#### {name}", "", "```diff", *chunk, "```"]
    if omitted:
        lines += ["", f"... {omitted} more changed lines omitted from the PR body."]
    return lines


def _escape_inline(text: str) -> str:
    """Control characters become their escape spellings: a row holding raw
    newlines, tabs or NULs must stay one fence line and must not carry them
    raw into the PR body."""
    out = []
    for ch in text:
        if ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\0":
            out.append("\\0")
        elif ord(ch) < 0x20:
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return "".join(out)


def _fence_for(lines: list[str]) -> str:
    """One backtick past the longest run the kept lines hold, three at minimum:
    an inner ``` run must not close the fence early."""
    longest = 0
    for line in lines:
        run = 0
        for ch in line:
            run = run + 1 if ch == "`" else 0
            longest = max(longest, run)
    return "`" * max(3, longest + 1)


def _render_strings(version: str, diff: dict[str, list] | None) -> list[str]:
    """One release's strings section: per-class counts plus the inline rows."""
    if diff is None:
        return [f"strings baseline recorded in archive/{version}/strings.jsonl."]
    if not diff or all(not diff[name] for name in ("added", "changed", "removed")):
        return ["no harness-string changes."]

    counts = {name: [0, 0, 0] for name in RULE_CLASSES}

    def count_row(row: dict, column: int) -> None:
        for name in row["rules"]:
            if name in counts:
                counts[name][column] += 1

    for row in diff["added"]:
        count_row(row, 0)
    for old, new in diff["changed"]:
        for name in set(old["rules"]) | set(new["rules"]):
            if name in counts:
                counts[name][1] += 1
    for row in diff["removed"]:
        count_row(row, 2)

    lines = [
        "#### strings",
        "",
        "| rule class | added | changed | removed |",
        "| --- | --- | --- | --- |",
    ]
    for name in RULE_CLASSES:
        class_added, class_changed, class_removed = counts[name]
        if class_added or class_changed or class_removed:
            lines.append(f"| {name} | {class_added} | {class_changed} | {class_removed} |")

    metadata_count = sum(1 for old, new in diff["changed"] if old["text"] == new["text"])
    if metadata_count:
        noun = "row" if metadata_count == 1 else "rows"
        lines.append(f"{metadata_count} {noun} changed only occurs or rules.")
    if diff.get("pairing_skipped"):
        lines.append(
            f"pairing skipped: the unmatched rows exceed the {PAIRING_MAX_PAIRS}-pair "
            "budget, so they render as plain additions and removals."
        )

    def rides_inline(row: dict) -> bool:
        return any(name in INLINE_RULES for name in row["rules"])

    units: list[list[str]] = []
    for old, new in sorted(diff["changed"], key=lambda pair: pair[0]["text"]):
        if old["text"] == new["text"]:
            # a metadata-only pair changes no bytes: the counts carry it
            continue
        if rides_inline(old) or rides_inline(new):
            units.append([f"-{_escape_inline(old['text'])}", f"+{_escape_inline(new['text'])}"])
    for row in sorted(diff["added"], key=_by_text):
        if rides_inline(row):
            units.append([f"+{_escape_inline(row['text'])}"])
    for row in sorted(diff["removed"], key=_by_text):
        if rides_inline(row):
            units.append([f"-{_escape_inline(row['text'])}"])

    budget = STRINGS_MAX_CHARS
    kept: list[str] = []
    dropped = 0
    for unit in units:
        cost = sum(len(line) + 1 for line in unit)
        if cost > budget:
            # a unit drops whole — a lone - line would report a removal whose
            # replacement the body never shows — but later units still render
            dropped += len(unit)
            continue
        kept += unit
        budget -= cost

    if kept:
        fence = _fence_for(kept)
        lines += ["", f"{fence}diff", *kept, fence]
    if dropped:
        lines.append(f"... {dropped} more diff lines omitted from the PR body.")
    lines += ["", f"full rows: `archive/{version}/strings.jsonl`"]
    return lines


def _report(
    watermark: str,
    rows: list[tuple[str, dict[str, tuple[list[str], int, int]], dict[str, list] | None]],
) -> str:
    captured = ", ".join(version for version, _, _ in rows)
    lines = [f"captured CC {captured}; `captures/` held {watermark}."]
    body_chars = len(lines[0]) + 1
    dropped: list[str] = []
    for index, (version, diffs, sdiff) in enumerate(rows):
        section = ["", *_render_changes(version, diffs), "", *_render_strings(version, sdiff)]
        section_chars = sum(len(line) + 1 for line in section)
        # the first section renders whatever it costs; a later one must fit
        # whole, never cut mid-fence
        if index and body_chars + section_chars > MAX_REPORT_CHARS:
            dropped.append(version)
            continue
        lines += section
        body_chars += section_chars
    lines += [
        "",
        "adjacent `archive/` dirs differ only in `meta.json` and `strings.jsonl` "
        "when a release changed no prompt bytes.",
    ]
    if dropped:
        noun = "section" if len(dropped) == 1 else "sections"
        lines.append(
            f"... {len(dropped)} more release {noun} omitted from the PR body: "
            f"the PR body budget is spent ({', '.join(dropped)})."
        )
    return "\n".join(lines) + "\n"


def _refresh_watermark(
    watermark: str, captures_dir: Path, archive_dir: Path, cache_dir: Path, base_url: str
) -> str:
    binary = install_binary(watermark, cache_dir, base_url)
    tmp = Path(tempfile.mkdtemp(prefix=f"cc-prompts-{watermark}-"))
    try:
        capture_set(str(binary), tmp)
        diffs = changed_diffs(captures_dir, tmp)
        if not diffs:
            return f"no new CC releases; the committed capture of CC {watermark} is unchanged."
        dest = archive_dir / watermark
        dest.mkdir(parents=True, exist_ok=True)
        for path in tmp.glob("*.md"):
            shutil.copy2(path, captures_dir / path.name)
            shutil.copy2(path, dest / path.name)
        for root in (captures_dir, dest):
            shutil.copy2(tmp / META_NAME, root / META_NAME)
        section = "\n".join(_render_changes(watermark, diffs))
        return (
            f"no new CC releases; CC {watermark} re-captured with changed prompts "
            f"({', '.join(diffs)}).\n\n{section}\n"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _rows_from_jsonl(text: str, source: Path | str = "baseline") -> list[dict]:
    """Baseline rows: `rules` is comma-joined in the file, a list in memory.

    Hand-edited baselines fail loudly: whitespace-only lines are skipped, a
    non-string rules field or a repeated row text raises naming the file.
    """
    rows = []
    seen: set[str] = set()
    for line in text.split("\n"):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row["rules"], str):
            raise RuntimeError(f"{source}: row {row['text']!r} carries a non-string rules field")
        if row["text"] in seen:
            raise RuntimeError(f"{source}: duplicate row text {row['text']!r}")
        seen.add(row["text"])
        row["rules"] = [name for name in row["rules"].split(",") if name]
        rows.append(row)
    return rows


def _strings_baseline(archive_dir: Path, version: str) -> Path | None:
    """Newest stored strings.jsonl under a release older than `version`.

    A candidate dir must also carry the release's meta sidecar: only a complete
    snapshot is a baseline this pipeline wrote, and a stray or partially
    written strings.jsonl must not poison the diff.
    """
    current = version_key(version)
    stored = [
        path
        for path in archive_dir.glob("*/strings.jsonl")
        if (path.parent / META_NAME).exists()
        and VERSION_DIR_RE.match(path.parent.name)
        and version_key(path.parent.name) < current
    ]
    return max(stored, key=lambda path: version_key(path.parent.name)) if stored else None


def _capture_versions(
    versions: list[str],
    watermark: str,
    captures_dir: Path,
    archive_dir: Path,
    cache_dir: Path,
    base_url: str,
) -> str:
    prev = captures_dir
    rows: list[tuple[str, dict[str, tuple[list[str], int, int]], dict[str, list] | None]] = []
    for version in versions:
        binary = install_binary(version, cache_dir, base_url)
        cur_rows = extract(binary)
        strings = render_jsonl(cur_rows)
        tmp = Path(tempfile.mkdtemp(prefix=f"cc-prompts-{version}-"))
        try:
            capture_set(str(binary), tmp)
            dest = archive_dir / version
            dest.mkdir(parents=True, exist_ok=True)
            for path in tmp.glob("*.md"):
                shutil.copy2(path, dest / path.name)
            shutil.copy2(tmp / META_NAME, dest / META_NAME)
            baseline = _strings_baseline(archive_dir, version)
            baseline_text = baseline.read_text() if baseline else None
            if baseline_text is None:
                (dest / "strings.jsonl").write_text(strings)
                sdiff = None
            else:
                if baseline_text != strings:
                    (dest / "strings.jsonl").write_text(strings)
                # an unchanged release pays a string compare, never the pairing
                sdiff = (
                    {}
                    if baseline_text == strings
                    else strings_diff(_rows_from_jsonl(baseline_text, baseline), cur_rows)
                )
            rows.append((version, changed_diffs(prev, dest), sdiff))
            prev = dest
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    # the newest set is the artifact of record in captures/
    for path in prev.glob("*.md"):
        shutil.copy2(path, captures_dir / path.name)
    shutil.copy2(prev / META_NAME, captures_dir / META_NAME)
    return _report(watermark, rows)


def refresh(
    repo: Path,
    cache_dir: Path,
    changelog_url: str = CHANGELOG_URL,
    base_url: str = DEFAULT_BASE_URL,
) -> str:
    captures_dir = repo / "captures"
    archive_dir = repo / "archive"
    watermark = parse_watermark(captures_dir / META_NAME)
    versions = versions_since(fetch_changelog(changelog_url), watermark)
    if not versions:
        return _refresh_watermark(watermark, captures_dir, archive_dir, cache_dir, base_url)
    return _capture_versions(versions, watermark, captures_dir, archive_dir, cache_dir, base_url)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="checkout holding captures/")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "cc-prompts-bin",
        help="where to keep downloaded binaries (default: the temp dir)",
    )
    parser.add_argument("--changelog", default=CHANGELOG_URL)
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="write the PR body here (default: <repo>/drift-report.md)",
    )
    args = parser.parse_args(argv)

    body = refresh(args.repo, args.cache_dir, args.changelog)
    report_path = args.report or args.repo / REPORT_PATH
    report_path.write_text(body)
    print(body, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
