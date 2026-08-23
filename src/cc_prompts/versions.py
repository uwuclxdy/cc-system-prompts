"""Enumerate released Claude Code versions from the public changelog.

The changelog at anthropics/claude-code carries one `## x.y.z` heading per
published release, so it doubles as the version list. `downloads.claude.ai`
still serves the binary for every version listed, which is what the
multi-version capture loops over.
"""

import argparse
import re
import sys
import urllib.request
from pathlib import Path

CHANGELOG_URL = "https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md"
FETCH_TIMEOUT = 30
# one heading per release, plain semver; everything else is prose or notes
VERSION_RE = re.compile(r"^##\s+(\d+\.\d+\.\d+)\s*$")


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def parse_versions(text: str) -> list[str]:
    """Release versions in ascending order, deduped, fenced code blocks skipped."""
    versions: set[str] = set()
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if match := VERSION_RE.match(line):
            versions.add(match.group(1))
    return sorted(versions, key=_key)


def versions_since(text: str, watermark: str) -> list[str]:
    """Versions strictly newer than the watermark, oldest first."""
    return [v for v in parse_versions(text) if _key(v) > _key(watermark)]


def fetch_changelog(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT) as response:
            return response.read().decode()
    except OSError as error:
        # a missing list must not read as "no new versions": the watermark only
        # advances on a landed capture, so the skipped releases would vanish for good
        raise RuntimeError(f"failed to fetch the version list from {url}: {error}") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True, help="print versions strictly newer than this")
    parser.add_argument("--changelog", default=CHANGELOG_URL, help="URL or path of the changelog")
    args = parser.parse_args(argv)

    if "://" in args.changelog:
        text = fetch_changelog(args.changelog)
    else:
        text = Path(args.changelog).read_text()
    for version in versions_since(text, args.since):
        print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
