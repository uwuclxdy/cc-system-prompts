"""The set-level provenance sidecar `meta.json`.

A capture `.md` holds prompt text only. The observation date, CC version and
per-file model (plus the subagent note) live in one `meta.json` per set:
`captures/meta.json`, plus one snapshot per `archive/<version>/`.
"""

import json
import re
from datetime import date
from pathlib import Path

META_NAME = "meta.json"
VERSION_RE = re.compile(r"\d+(\.\d+)+")


def parse_watermark(meta_path: Path) -> str:
    """The CC version a committed set was captured with."""
    try:
        version = json.loads(meta_path.read_text()).get("version")
    except (OSError, ValueError) as error:
        raise RuntimeError(f"unreadable provenance sidecar {meta_path}") from error
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise RuntimeError(f"no CC version in the provenance sidecar {meta_path}")
    return version


def record_capture(out_dir: Path, name: str, model_id: str, version: str, note: str = "") -> None:
    """Add one capture's provenance entry, creating the sidecar on first use."""
    meta_path = out_dir / META_NAME
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        meta = {}
    if meta.get("version") != version:
        # a fresh set; never mix two releases in one sidecar
        meta = {"version": version, "observed": date.today().isoformat(), "captures": {}}
    entry = {"model": model_id}
    if note:
        entry["note"] = note
    meta["captures"][name] = entry
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
