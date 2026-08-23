"""Install a Claude Code release binary from the official distribution CDN.

The installer script downloads the latest binary and checksums it against the
release manifest; this does the same for any version the changelog lists, so a
capture pins the exact release it records. A cached binary is reused while its
checksum still matches the manifest.
"""

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "https://downloads.claude.ai/claude-code-releases"
FETCH_TIMEOUT = 60
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_BLOCK = 1024 * 1024


def detect_platform() -> str:
    """The manifest key for this machine, spelled the way install.sh spells it."""
    system = platform.system()
    machine = platform.machine()
    if system == "Linux":
        arch = "arm64" if machine in ("aarch64", "arm64") else "x64"
        libc, _ = platform.libc_ver()
        return f"linux-{arch}-musl" if libc == "musl" else f"linux-{arch}"
    if system == "Darwin":
        return f"darwin-{'arm64' if machine == 'arm64' else 'x64'}"
    if system == "Windows":
        return f"win32-{'arm64' if machine == 'arm64' else 'x64'}"
    raise RuntimeError(f"unsupported platform: {system} {machine}")


def _fetch(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT) as response:
            return response.read()
    except OSError as error:
        raise RuntimeError(f"failed to fetch {url}: {error}") from error


def _manifest_checksum(version: str, base_url: str, platform_key: str) -> str:
    manifest = json.loads(_fetch(f"{base_url}/{version}/manifest.json"))
    entry = manifest.get("platforms", {}).get(platform_key)
    if entry is None:
        raise RuntimeError(f"release {version} has no manifest entry for {platform_key}")
    checksum = entry.get("checksum", "")
    if not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise RuntimeError(f"release {version} carries no valid checksum for {platform_key}")
    return checksum


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def install_binary(version: str, cache_dir: Path, base_url: str = DEFAULT_BASE_URL) -> Path:
    """Return the release binary, downloading and checksum-verifying it when needed."""
    if not VERSION_RE.match(version):
        # the value reaches a filename, so a bad one must never reach the cache dir
        raise RuntimeError(f"not a release version: {version!r}")
    platform_key = detect_platform()
    checksum = _manifest_checksum(version, base_url, platform_key)
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / version
    if target.exists() and _sha256(target) == checksum:
        return target
    partial = cache_dir / f".{version}.part"
    with (
        urllib.request.urlopen(
            f"{base_url}/{version}/{platform_key}/claude", timeout=FETCH_TIMEOUT
        ) as response,
        partial.open("wb") as handle,
    ):
        shutil.copyfileobj(response, handle)
    if _sha256(partial) != checksum:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"release {version} failed checksum verification for {platform_key}")
    partial.chmod(0o755)
    os.replace(partial, target)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="release to install, e.g. 2.1.241")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "cc-prompts-bin",
        help="where to keep downloaded binaries (default: the temp dir)",
    )
    args = parser.parse_args(argv)

    print(install_binary(args.version, args.cache_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
