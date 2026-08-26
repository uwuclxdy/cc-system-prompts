import json
from datetime import date

import pytest

from cc_prompts.meta import parse_watermark, record_capture


def test_record_capture_creates_the_sidecar(tmp_path):
    record_capture(tmp_path, "opus.md", "claude-opus-5", "2.1.241")
    assert json.loads((tmp_path / "meta.json").read_text()) == {
        "version": "2.1.241",
        "observed": date.today().isoformat(),
        "captures": {"opus.md": {"model": "claude-opus-5"}},
    }


def test_record_capture_merges_entries_of_the_same_version(tmp_path):
    record_capture(tmp_path, "opus.md", "claude-opus-5", "2.1.241")
    record_capture(tmp_path, "subagent.md", "claude-opus-5", "2.1.241", "subagent")
    meta = json.loads((tmp_path / "meta.json").read_text())
    assert meta["version"] == "2.1.241"
    assert meta["captures"] == {
        "opus.md": {"model": "claude-opus-5"},
        "subagent.md": {"model": "claude-opus-5", "note": "subagent"},
    }


def test_record_capture_restarts_the_sidecar_on_a_new_version(tmp_path):
    record_capture(tmp_path, "opus.md", "claude-opus-5", "2.1.241")
    record_capture(tmp_path, "opus.md", "claude-opus-5", "2.1.242")
    meta = json.loads((tmp_path / "meta.json").read_text())
    assert meta["version"] == "2.1.242"
    assert list(meta["captures"]) == ["opus.md"]


def test_parse_watermark_reads_the_sidecar(tmp_path):
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({"version": "2.1.241", "captures": {}}))
    assert parse_watermark(meta) == "2.1.241"


def test_parse_watermark_refuses_a_missing_sidecar(tmp_path):
    with pytest.raises(RuntimeError, match="provenance sidecar"):
        parse_watermark(tmp_path / "meta.json")


def test_parse_watermark_refuses_a_sidecar_without_a_version(tmp_path):
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({"observed": "2026-08-23"}))
    with pytest.raises(RuntimeError, match="provenance sidecar"):
        parse_watermark(meta)


def test_parse_watermark_rejects_a_version_of_the_wrong_shape(tmp_path):
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({"version": "latest"}))
    with pytest.raises(RuntimeError, match="provenance sidecar"):
        parse_watermark(meta)
