"""Re-capture every CC release since the last committed capture.

`captures/` always holds the newest set, and every release gets its own
`archive/<version>/` snapshot, so adjacent archive dirs diff to a readable
prompt change. The version list is the public changelog; the watermark is the
`version` field of `captures/meta.json`. A run with no new releases
re-captures the watermark version and rewrites only what drifted.
"""

import argparse
import difflib
import shutil
import sys
import tempfile
from pathlib import Path

from .capture import main as capture_main
from .install import DEFAULT_BASE_URL, install_binary
from .meta import META_NAME, parse_watermark
from .subagent import main as subagent_main
from .versions import CHANGELOG_URL, fetch_changelog, versions_since

REPORT_PATH = "drift-report.md"
# GitHub caps a PR body at 65536 chars; keep the changed-line section far below
MAX_DIFF_CHARS = 45_000


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


def _report(watermark: str, rows: list[tuple[str, dict[str, tuple[list[str], int, int]]]]) -> str:
    captured = ", ".join(version for version, _ in rows)
    lines = [f"captured CC {captured}; `captures/` held {watermark}."]
    for version, diffs in rows:
        lines += ["", *_render_changes(version, diffs)]
    lines += [
        "",
        "adjacent `archive/` dirs differ only in `meta.json` when a release "
        "changed no prompt bytes.",
    ]
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


def _capture_versions(
    versions: list[str],
    watermark: str,
    captures_dir: Path,
    archive_dir: Path,
    cache_dir: Path,
    base_url: str,
) -> str:
    prev = captures_dir
    rows: list[tuple[str, dict[str, tuple[list[str], int, int]]]] = []
    for version in versions:
        binary = install_binary(version, cache_dir, base_url)
        tmp = Path(tempfile.mkdtemp(prefix=f"cc-prompts-{version}-"))
        try:
            capture_set(str(binary), tmp)
            dest = archive_dir / version
            dest.mkdir(parents=True, exist_ok=True)
            for path in tmp.glob("*.md"):
                shutil.copy2(path, dest / path.name)
            shutil.copy2(tmp / META_NAME, dest / META_NAME)
            rows.append((version, changed_diffs(prev, dest)))
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
