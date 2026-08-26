import json
from pathlib import Path

import pytest

from cc_prompts.refresh import MAX_DIFF_CHARS, _render_changes, changed_diffs, refresh


def make_set(dest: Path, version: str, body: str = "prompt body\n") -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("opus.md", "sonnet.md", "subagent.md"):
        (dest / name).write_text(body)
    (dest / "meta.json").write_text(
        json.dumps({"version": version, "observed": "2026-08-23", "captures": {}})
    )


def make_repo(tmp_path: Path, version: str, body: str = "prompt body\n") -> Path:
    repo = tmp_path / "repo"
    make_set(repo / "captures", version, body)
    return repo


def stub_install(monkeypatch):
    def fake_install(version, cache_dir, base_url):
        return Path(f"/stub/{version}")

    monkeypatch.setattr("cc_prompts.refresh.install_binary", fake_install)


def stub_capture(monkeypatch, bodies: dict[str, str]):
    def fake_capture(binary, out):
        version = Path(binary).name
        make_set(out, version, bodies.get(version, "prompt body\n"))

    monkeypatch.setattr("cc_prompts.refresh.capture_set", fake_capture)


def test_changed_diffs_reports_nothing_for_equal_bodies(tmp_path):
    prev, cur = tmp_path / "prev", tmp_path / "cur"
    make_set(prev, "2.1.240")
    make_set(cur, "2.1.241")
    assert changed_diffs(prev, cur) == {}


def test_changed_diffs_counts_changed_lines_without_context(tmp_path):
    prev, cur = tmp_path / "prev", tmp_path / "cur"
    make_set(prev, "2.1.240", body="a\nb\nc\n")
    make_set(cur, "2.1.241", body="a\nx\nc\n")
    lines, added, removed = changed_diffs(prev, cur)["opus.md"]
    assert lines == ["-b", "+x"]
    assert (added, removed) == (1, 1)


def test_changed_diffs_treats_a_new_file_as_all_added(tmp_path):
    prev, cur = tmp_path / "prev", tmp_path / "cur"
    make_set(prev, "2.1.240")
    make_set(cur, "2.1.241")
    (cur / "fable.md").write_text("fable body\n")
    lines, added, removed = changed_diffs(prev, cur)["fable.md"]
    assert lines == ["+fable body"]
    assert (added, removed) == (1, 0)


def test_refresh_archives_every_new_release_and_advances_captures(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "2.1.240")
    monkeypatch.setattr(
        "cc_prompts.refresh.fetch_changelog",
        lambda url: "## 2.1.242\n## 2.1.241\n## 2.1.240\n",
    )
    stub_install(monkeypatch)
    stub_capture(monkeypatch, {"2.1.242": "changed\n"})

    body = refresh(repo, tmp_path / "cache")

    assert (repo / "archive" / "2.1.241" / "opus.md").read_text() == "prompt body\n"
    assert (repo / "archive" / "2.1.242" / "opus.md").read_text() == "changed\n"
    assert "changed\n" in (repo / "captures" / "opus.md").read_text()
    assert "captured CC 2.1.241, 2.1.242" in body
    assert "### 2.1.241" in body
    assert "no prompt changes." in body
    assert "| `opus.md` | +1 / -1 |" in body
    assert "-prompt body" in body
    assert "+changed" in body
    assert json.loads((repo / "captures" / "meta.json").read_text())["version"] == "2.1.242"
    assert (
        json.loads((repo / "archive" / "2.1.241" / "meta.json").read_text())["version"] == "2.1.241"
    )


def test_refresh_with_no_new_release_reports_unchanged(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "2.1.240")
    monkeypatch.setattr("cc_prompts.refresh.fetch_changelog", lambda url: "## 2.1.240\n")
    stub_install(monkeypatch)
    stub_capture(monkeypatch, {})

    body = refresh(repo, tmp_path / "cache")

    assert "unchanged" in body
    assert not (repo / "archive").exists()
    assert (repo / "captures" / "opus.md").read_text() == "prompt body\n"
    assert json.loads((repo / "captures" / "meta.json").read_text())["version"] == "2.1.240"


def test_refresh_with_no_new_release_rewrites_only_what_drifted(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "2.1.240")
    monkeypatch.setattr("cc_prompts.refresh.fetch_changelog", lambda url: "## 2.1.240\n")
    stub_install(monkeypatch)
    stub_capture(monkeypatch, {"2.1.240": "drifted\n"})

    body = refresh(repo, tmp_path / "cache")

    assert "re-captured with changed prompts (opus.md, sonnet.md, subagent.md)" in body
    assert (repo / "captures" / "opus.md").read_text() == "drifted\n"
    assert (repo / "archive" / "2.1.240" / "opus.md").read_text() == "drifted\n"
    assert "-prompt body" in body
    assert "+drifted" in body
    assert (
        json.loads((repo / "archive" / "2.1.240" / "meta.json").read_text())["version"] == "2.1.240"
    )


def test_refresh_refuses_to_run_without_a_watermark(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "2.1.240")
    (repo / "captures" / "meta.json").write_text("not json")
    monkeypatch.setattr("cc_prompts.refresh.fetch_changelog", lambda url: "## 2.1.240\n")

    with pytest.raises(RuntimeError, match="provenance sidecar"):
        refresh(repo, tmp_path / "cache")


def test_render_changes_caps_the_diff_section():
    # the whole 600-line change costs ~62K chars, far past the PR body budget
    line = "x" * 100
    diff_lines = [f"-{line}{i}" for i in range(600)]
    section = "\n".join(_render_changes("2.1.242", {"opus.md": (diff_lines, 600, 600)}))
    assert "| `opus.md` | +600 / -600 |" in section
    assert "more changed lines omitted from the PR body." in section
    assert section.count("-" + line) < len(diff_lines)
    assert sum(len(diff) + 1 for diff in diff_lines) > MAX_DIFF_CHARS


def test_render_changes_emits_no_blocks_for_an_unchanged_release():
    assert "\n".join(_render_changes("2.1.241", {})) == "### 2.1.241\n\nno prompt changes."
