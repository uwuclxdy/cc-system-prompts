"""Re-capture every CC release since the last committed capture.

`captures/` always holds the newest set, and every release gets its own
`archive/<version>/` snapshot, so adjacent archive dirs diff to a readable
prompt change. The version list is the public changelog; the watermark is the
provenance header of the committed capture. A run with no new releases
re-captures the watermark version and rewrites only what drifted.
"""

import argparse
import re
import shutil
import sys
import tempfile
from pathlib import Path

from .capture import main as capture_main
from .install import DEFAULT_BASE_URL, install_binary
from .subagent import main as subagent_main
from .versions import CHANGELOG_URL, fetch_changelog, versions_since

REPORT_PATH = "drift-report.md"
# `observed <date> (wire capture, CC <version>, <model>[, <note>])`
WATERMARK_RE = re.compile(r"wire capture, CC ([0-9][0-9.]*),")
# the provenance header always moves between versions (version + date), so a
# body comparison is what tells a prompt change apart from a version bump
HEADER_LINES = 2


def parse_watermark(path: Path) -> str:
    match = WATERMARK_RE.search(path.read_text().splitlines()[0])
    if match is None:
        raise RuntimeError(f"no CC version in the provenance header of {path}")
    return match.group(1)


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


def body_without_header(path: Path) -> str:
    return "\n".join(path.read_text().splitlines()[HEADER_LINES:])


def changed_files(prev: Path, cur: Path) -> list[str]:
    """Files whose prompt body differs; the provenance header never counts."""
    changed: list[str] = []
    for path in sorted(cur.glob("*.md")):
        other = prev / path.name
        if not other.exists() or body_without_header(other) != body_without_header(path):
            changed.append(path.name)
    return changed


def _report(watermark: str, rows: list[tuple[str, list[str]]]) -> str:
    captured = ", ".join(version for version, _ in rows)
    lines = [
        f"captured CC {captured}; `captures/` held {watermark}.",
        "",
        "| release | prompt change vs previous |",
        "| --- | --- |",
    ]
    for version, changed in rows:
        lines.append(f"| {version} | {', '.join(changed) if changed else 'none'} |")
    lines += [
        "",
        "adjacent `archive/` dirs differ in the provenance header only when a "
        "release changed no prompt bytes.",
    ]
    return "\n".join(lines) + "\n"


def _refresh_watermark(
    watermark: str, captures_dir: Path, archive_dir: Path, cache_dir: Path, base_url: str
) -> str:
    binary = install_binary(watermark, cache_dir, base_url)
    tmp = Path(tempfile.mkdtemp(prefix=f"cc-prompts-{watermark}-"))
    try:
        capture_set(str(binary), tmp)
        changed = changed_files(captures_dir, tmp)
        if not changed:
            return f"no new CC releases; the committed capture of CC {watermark} is unchanged."
        dest = archive_dir / watermark
        dest.mkdir(parents=True, exist_ok=True)
        for path in tmp.glob("*.md"):
            shutil.copy2(path, captures_dir / path.name)
            shutil.copy2(path, dest / path.name)
        return (
            f"no new CC releases; CC {watermark} re-captured with changed prompts "
            f"({', '.join(changed)})."
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
    rows: list[tuple[str, list[str]]] = []
    for version in versions:
        binary = install_binary(version, cache_dir, base_url)
        tmp = Path(tempfile.mkdtemp(prefix=f"cc-prompts-{version}-"))
        try:
            capture_set(str(binary), tmp)
            dest = archive_dir / version
            dest.mkdir(parents=True, exist_ok=True)
            for path in tmp.glob("*.md"):
                shutil.copy2(path, dest / path.name)
            rows.append((version, changed_files(prev, dest)))
            prev = dest
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    # the newest set is the artifact of record in captures/
    for path in prev.glob("*.md"):
        shutil.copy2(path, captures_dir / path.name)
    return _report(watermark, rows)


def refresh(
    repo: Path,
    cache_dir: Path,
    changelog_url: str = CHANGELOG_URL,
    base_url: str = DEFAULT_BASE_URL,
) -> str:
    captures_dir = repo / "captures"
    archive_dir = repo / "archive"
    watermark = parse_watermark(captures_dir / "opus.md")
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
