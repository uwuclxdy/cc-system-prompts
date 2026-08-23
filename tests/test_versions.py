import pytest

from cc_prompts.versions import fetch_changelog, parse_versions, versions_since

CHANGELOG = """# Changelog

## 2.1.242

- notes for the newest release

## 2.1.241

- notes

```md
## 2.1.999
```

## 2.1.10

## 2.1.9

## 2.1.0

## 1.0.5
"""


def test_parse_versions_orders_numerically_and_skips_fenced_headings():
    assert parse_versions(CHANGELOG) == [
        "1.0.5",
        "2.1.0",
        "2.1.9",
        "2.1.10",
        "2.1.241",
        "2.1.242",
    ]


def test_parse_versions_dedupes_repeated_headings():
    assert parse_versions("## 2.1.1\n## 2.1.1\n") == ["2.1.1"]


def test_versions_since_compares_parts_not_digits():
    assert versions_since(CHANGELOG, "2.1.9") == ["2.1.10", "2.1.241", "2.1.242"]


def test_versions_since_excludes_the_watermark_itself():
    assert versions_since(CHANGELOG, "2.1.242") == []


def test_fetch_changelog_names_the_url_when_it_fails(monkeypatch):
    def boom(url, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr("cc_prompts.versions.urllib.request.urlopen", boom)
    with pytest.raises(RuntimeError, match="version list from https://example"):
        fetch_changelog("https://example/changelog.md")
