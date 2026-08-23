from pathlib import Path

import pytest

from cc_prompts.refresh import changed_files, parse_watermark, refresh


def header(version: str) -> str:
    return f"observed 2026-08-23 (wire capture, CC {version}, claude-opus-5)\n\n"


def make_set(dest: Path, version: str, body: str = "prompt body\n") -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("opus.md", "sonnet.md", "subagent.md"):
        (dest / name).write_text(header(version) + body + "\n")


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


def test_parse_watermark_reads_the_provenance_header(tmp_path):
    path = tmp_path / "opus.md"
    path.write_text(header("2.1.241"))
    assert parse_watermark(path) == "2.1.241"


def test_parse_watermark_refuses_a_header_without_a_version(tmp_path):
    path = tmp_path / "opus.md"
    path.write_text("observed 2026-08-23 (wire capture)\n\nbody\n")
    with pytest.raises(RuntimeError, match="provenance header"):
        parse_watermark(path)


def test_changed_files_ignores_the_provenance_header(tmp_path):
    prev, cur = tmp_path / "prev", tmp_path / "cur"
    make_set(prev, "2.1.240")
    make_set(cur, "2.1.241")
    assert changed_files(prev, cur) == []
    (cur / "opus.md").write_text(header("2.1.241") + "new body\n")
    assert changed_files(prev, cur) == ["opus.md"]


def test_refresh_archives_every_new_release_and_advances_captures(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "2.1.240")
    monkeypatch.setattr(
        "cc_prompts.refresh.fetch_changelog",
        lambda url: "## 2.1.242\n## 2.1.241\n## 2.1.240\n",
    )
    stub_install(monkeypatch)
    stub_capture(monkeypatch, {"2.1.242": "changed\n"})

    body = refresh(repo, tmp_path / "cache")

    assert "2.1.241" in (repo / "archive" / "2.1.241" / "opus.md").read_text()
    assert "2.1.242" in (repo / "archive" / "2.1.242" / "opus.md").read_text()
    assert "changed\n" in (repo / "captures" / "opus.md").read_text()
    assert "captured CC 2.1.241, 2.1.242" in body
    assert "| 2.1.241 | none |" in body
    assert "| 2.1.242 | opus.md, sonnet.md, subagent.md |" in body


def test_refresh_with_no_new_release_reports_unchanged(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "2.1.240")
    monkeypatch.setattr("cc_prompts.refresh.fetch_changelog", lambda url: "## 2.1.240\n")
    stub_install(monkeypatch)
    stub_capture(monkeypatch, {})

    body = refresh(repo, tmp_path / "cache")

    assert "unchanged" in body
    assert not (repo / "archive").exists()
    assert "2.1.240" in (repo / "captures" / "opus.md").read_text()


def test_refresh_with_no_new_release_rewrites_only_what_drifted(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "2.1.240")
    monkeypatch.setattr("cc_prompts.refresh.fetch_changelog", lambda url: "## 2.1.240\n")
    stub_install(monkeypatch)
    stub_capture(monkeypatch, {"2.1.240": "drifted\n"})

    body = refresh(repo, tmp_path / "cache")

    assert "re-captured with changed prompts (opus.md, sonnet.md, subagent.md)" in body
    assert "drifted\n" in (repo / "captures" / "opus.md").read_text()
    assert "drifted\n" in (repo / "archive" / "2.1.240" / "opus.md").read_text()


def test_refresh_refuses_to_run_without_a_watermark(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "2.1.240")
    (repo / "captures" / "opus.md").write_text("no provenance here\n")
    monkeypatch.setattr("cc_prompts.refresh.fetch_changelog", lambda url: "## 2.1.240\n")

    with pytest.raises(RuntimeError, match="provenance header"):
        refresh(repo, tmp_path / "cache")
