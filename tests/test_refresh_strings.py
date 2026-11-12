import json
from pathlib import Path

import pytest

from cc_prompts.refresh import (
    MAX_REPORT_CHARS,
    STRINGS_MAX_CHARS,
    _render_strings,
    _report,
    _rows_from_jsonl,
    refresh,
    strings_diff,
)
from cc_prompts.strings_extract import extract, render_jsonl
from fixture_binary import (
    EXPECTED_ROWS,
    REPLACED_TEXT,
    REPLACEMENT_TEXT,
    build_binary,
    row_text_key,
)
from test_refresh import make_repo, stub_capture


def strings_at(path: Path) -> list[dict]:
    lines = path.read_text().split("\n")[:-1]
    return [json.loads(line) for line in lines]


def stub_install(monkeypatch, binaries: dict[str, Path]) -> None:
    def fake_install(version, cache_dir, base_url):
        return binaries[version]

    monkeypatch.setattr("cc_prompts.refresh.install_binary", fake_install)


def test_refresh_writes_strings_jsonl_per_release(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "2.1.240")
    monkeypatch.setattr(
        "cc_prompts.refresh.fetch_changelog",
        lambda url: "## 2.1.242\n## 2.1.241\n",
    )
    binaries = {
        "2.1.241": build_binary(tmp_path / "b241.bin"),
        "2.1.242": build_binary(tmp_path / "b242.bin", reword_search_hint=True),
    }
    stub_install(monkeypatch, binaries)
    stub_capture(monkeypatch, {})

    body = refresh(repo, tmp_path / "cache")

    v1 = strings_at(repo / "archive" / "2.1.241" / "strings.jsonl")
    v2 = strings_at(repo / "archive" / "2.1.242" / "strings.jsonl")
    assert v1[0] == {
        "key": EXPECTED_ROWS[0]["key"],
        "text": EXPECTED_ROWS[0]["text"],
        "rules": "reminder",
        "occurs": 1,
    }
    assert len(v1) == len(EXPECTED_ROWS)
    assert len(v2) == len(EXPECTED_ROWS)
    v2_texts = {row["text"] for row in v2}
    assert REPLACEMENT_TEXT in v2_texts
    assert REPLACED_TEXT not in v2_texts
    assert "strings baseline recorded in archive/2.1.241/strings.jsonl." in body
    assert "#### strings" in body
    assert "| description | 0 | 1 | 0 |" in body
    assert f"-{REPLACED_TEXT}" in body
    assert f"+{REPLACEMENT_TEXT}" in body
    assert "full rows: `archive/2.1.242/strings.jsonl`" in body


def test_refresh_skips_the_strings_write_when_unchanged(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "2.1.240")
    monkeypatch.setattr(
        "cc_prompts.refresh.fetch_changelog",
        lambda url: "## 2.1.242\n## 2.1.241\n",
    )
    binaries = {
        "2.1.241": build_binary(tmp_path / "b241.bin"),
        "2.1.242": build_binary(tmp_path / "b242.bin", renamed_identifier=True),
    }
    stub_install(monkeypatch, binaries)
    stub_capture(monkeypatch, {})

    body = refresh(repo, tmp_path / "cache")

    assert (repo / "archive" / "2.1.241" / "strings.jsonl").exists()
    assert not (repo / "archive" / "2.1.242" / "strings.jsonl").exists()
    assert "no harness-string changes." in body


def test_refresh_diffs_against_the_last_stored_strings(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "2.1.240")
    # two fabricated older baselines pin the walk-back's two bounds: a
    # qualifying dir whose lexical order sorts above 2.1.241 (the walk-back
    # compares numerically, never lexically) and a numerically-newest dir with
    # no meta sidecar (an incomplete snapshot is no baseline)
    ancient = repo / "archive" / "2.1.9"
    ancient.mkdir(parents=True)
    (ancient / "strings.jsonl").write_text(
        json.dumps(
            {
                "key": row_text_key("ANCIENT BASELINE ONE"),
                "text": "ANCIENT BASELINE ONE",
                "rules": "hook",
                "occurs": 1,
            }
        )
        + "\n"
    )
    (ancient / "meta.json").write_text(json.dumps({"version": "2.1.9"}))
    metaless = repo / "archive" / "2.1.240"
    metaless.mkdir(parents=True)
    (metaless / "strings.jsonl").write_text(
        json.dumps(
            {
                "key": row_text_key("ANCIENT BASELINE TWO"),
                "text": "ANCIENT BASELINE TWO",
                "rules": "reminder",
                "occurs": 1,
            }
        )
        + "\n"
    )
    monkeypatch.setattr(
        "cc_prompts.refresh.fetch_changelog",
        lambda url: "## 2.1.243\n## 2.1.242\n## 2.1.241\n",
    )
    binaries = {
        "2.1.241": build_binary(tmp_path / "b241.bin"),
        # identical rows, different bytes: the skip must key on rows, not bytes
        "2.1.242": build_binary(tmp_path / "b242.bin", renamed_identifier=True),
        "2.1.243": build_binary(tmp_path / "b243.bin", reword_search_hint=True),
    }
    stub_install(monkeypatch, binaries)
    stub_capture(monkeypatch, {})

    body = refresh(repo, tmp_path / "cache")

    assert (repo / "archive" / "2.1.241" / "strings.jsonl").exists()
    assert not (repo / "archive" / "2.1.242" / "strings.jsonl").exists()
    assert (repo / "archive" / "2.1.243" / "strings.jsonl").exists()
    # 2.1.240's meta-less file is no baseline, so its reminder row never enters
    # any diff, and a reminder row rides inline when it does
    assert "ANCIENT BASELINE TWO" not in body
    # 2.1.243 diffs against 2.1.241's rows, never 2.1.9's: no hook change
    assert "| hook" not in body.split("### 2.1.243", 1)[1]
    assert "| description | 0 | 1 | 0 |" in body


def test_strings_diff_pairs_close_rows_and_leaves_the_rest_unmatched():
    keeper = {
        "key": "1" * 16,
        "text": "Keep this unchanged description text.",
        "rules": ["description"],
        "occurs": 1,
    }
    old_pair = {
        "key": "2" * 16,
        "text": "Rewrite the config file only after the backup copy completes successfully.",
        "rules": ["description"],
        "occurs": 1,
    }
    new_pair = {
        "key": "3" * 16,
        "text": "Rewrite the config file only after the backup copy completes with success.",
        "rules": ["description"],
        "occurs": 1,
    }
    gone = {
        "key": "4" * 16,
        "text": "Register a git hook that formats staged files.",
        "rules": ["hook"],
        "occurs": 1,
    }
    fresh = {
        "key": "5" * 16,
        "text": "Run a shell command now.",
        "rules": ["description"],
        "occurs": 1,
    }

    diff = strings_diff([old_pair, gone], [new_pair, fresh, keeper])

    assert diff["changed"] == [(old_pair, new_pair)]
    assert diff["removed"] == [gone]
    # unpaired rows are plain leftovers, sorted by text: keeper is new here and
    # must be counted, whatever its length similarity to a removed row
    assert diff["added"] == [keeper, fresh]


def test_render_strings_section_tables_nonzero_rule_counts_only():
    prev = [
        {
            "key": "2" * 16,
            "text": "Rewrite the config file only after the backup copy completes successfully.",
            "rules": ["description"],
            "occurs": 1,
        },
        {
            "key": "4" * 16,
            "text": "Register a git hook that formats staged files.",
            "rules": ["hook"],
            "occurs": 1,
        },
        {
            "key": "6" * 16,
            "text": "When you edit a file, do not reformat unrelated lines; keep the diff minimal.",
            "rules": ["prose"],
            "occurs": 1,
        },
    ]
    cur = [
        {
            "key": "3" * 16,
            "text": "Rewrite the config file only after the backup copy completes with success.",
            "rules": ["description"],
            "occurs": 1,
        },
        {
            "key": "5" * 16,
            "text": "Run a shell command now.",
            "rules": ["description"],
            "occurs": 1,
        },
        {
            "key": "6" * 16,
            "text": "When you edit a file, do not reformat unrelated lines; keep the diff minimal.",
            "rules": ["prose"],
            "occurs": 1,
        },
    ]

    section = "\n".join(_render_strings("2.1.242", strings_diff(prev, cur)))

    assert "| description | 1 | 1 | 0 |" in section
    assert "| hook | 0 | 0 | 1 |" in section
    assert "| prose" not in section
    assert "-Rewrite the config file only after the backup copy completes successfully." in section
    assert "+Rewrite the config file only after the backup copy completes with success." in section
    assert "+Run a shell command now." in section
    assert "-Register a git hook" not in section
    assert "full rows: `archive/2.1.242/strings.jsonl`" in section


def test_render_strings_section_omits_past_its_budget():
    prev, cur = [], []
    for i in range(150):
        prev.append(
            {
                "key": f"{i:016x}",
                "text": f"Use the Bash tool variant {i:03d} to edit every file in the tree.",
                "rules": ["description"],
                "occurs": 1,
            }
        )
        cur.append(
            {
                "key": f"{i:016x}",
                "text": f"Use the Bash tool variant {i:03d} to edit every file in the whole tree.",
                "rules": ["description"],
                "occurs": 1,
            }
        )

    diff = strings_diff(prev, cur)
    assert len(diff["changed"]) == 150
    assert diff["added"] == []
    assert diff["removed"] == []

    section = "\n".join(_render_strings("2.1.242", diff))

    assert "more diff lines omitted from the PR body." in section
    # some pairs must survive the budget: a zero budget or a dropped-fence
    # regression keeps this zero and greens vacuously
    assert 0 < section.count("+Use the Bash tool variant") < 150
    total = sum(len(f"-{row['text']}") + len(f"+{row['text']}") + 2 for row in prev)
    assert total > STRINGS_MAX_CHARS


def _row(key_seed: str, text: str, rules: list[str]) -> dict:
    return {"key": key_seed * 16, "text": text, "rules": rules, "occurs": 1}


def test_render_strings_section_escapes_inline_rows_and_sizes_the_fence():
    old = _row("a", "first line\nsecond ```line", ["description"])
    new = _row("b", "first line\nsecond ````line", ["description"])

    section = "\n".join(_render_strings("2.1.242", strings_diff([old], [new])))

    # the carved newlines render escaped: every kept row stays one fence line
    assert "-first line\\nsecond ```line" in section
    assert "+first line\\nsecond ````line" in section
    # the fence outruns the longest backtick run in the kept rows (4 -> 5)
    assert "`````diff" in section
    assert section.split("\n")[-1] == "full rows: `archive/2.1.242/strings.jsonl`"


def test_strings_diff_counts_metadata_only_changes():
    only_rules = (
        _row("a", "Same text both releases.", ["description"]),
        _row("a", "Same text both releases.", ["description", "prose"]),
    )
    only_occurs = (
        _row("b", "Same text, one more occurrence.", ["hook"]),
        _row("b", "Same text, one more occurrence.", ["hook"]),
    )
    only_occurs[1]["occurs"] = 2

    diff = strings_diff([only_rules[0], only_occurs[0]], [only_rules[1], only_occurs[1]])

    assert diff["changed"] == [(only_rules[0], only_rules[1]), (only_occurs[0], only_occurs[1])]
    assert diff["added"] == []
    assert diff["removed"] == []

    section = "\n".join(_render_strings("2.1.242", diff))

    assert "| description | 0 | 1 | 0 |" in section
    assert "| prose | 0 | 1 | 0 |" in section
    assert "| hook | 0 | 1 | 0 |" in section
    assert "2 rows changed only occurs or rules." in section
    assert "```diff" not in section

    single = strings_diff([only_rules[0]], [only_rules[1]])
    assert "1 row changed only occurs or rules." in "\n".join(_render_strings("2.1.242", single))


def test_render_strings_section_skips_the_fence_when_nothing_rides_inline():
    old = _row("a", "P" * 90 + " prose one", ["prose"])
    new = _row("b", "P" * 90 + " prose two", ["prose"])

    section = "\n".join(_render_strings("2.1.242", strings_diff([old], [new])))

    assert "| prose | 0 | 1 | 0 |" in section
    assert "```diff" not in section
    assert "full rows: `archive/2.1.242/strings.jsonl`" in section


def test_render_strings_section_keeps_a_pair_atomic_at_the_budget_edge():
    old = _row("a", "y" * 14990 + " old tail text", ["description"])
    new = _row("b", "y" * 14990 + " new tail text", ["description"])

    diff = strings_diff([old], [new])
    assert len(diff["changed"]) == 1

    section = "\n".join(_render_strings("2.1.242", diff))

    # a pair that cannot fit whole drops whole: a lone - line would render a
    # removal the release never made
    assert "```diff" not in section
    assert "2 more diff lines omitted from the PR body." in section


def test_render_strings_section_skips_an_oversized_unit_and_keeps_later_ones():
    giant_old = _row("a", "A" * 14990 + " old tail text", ["description"])
    giant_new = _row("b", "A" * 14990 + " new tail text", ["description"])
    small_old = _row("c", "Small old description line.", ["description"])
    small_new = _row("d", "Small new description line.", ["description"])

    diff = strings_diff([giant_old, small_old], [giant_new, small_new])
    assert len(diff["changed"]) == 2

    section = "\n".join(_render_strings("2.1.242", diff))

    # the oversized pair sorts first and drops, but units after it still render
    assert "+Small new description line." in section
    assert "-Small old description line." in section
    assert "2 more diff lines omitted from the PR body." in section


def test_strings_diff_skips_matcher_construction_outside_the_length_gate(monkeypatch):
    import difflib

    import cc_prompts.refresh

    built = []
    real_matcher = difflib.SequenceMatcher

    def counting(isjunk, a, b):
        built.append((a, b))
        return real_matcher(isjunk, a, b)

    monkeypatch.setattr(cc_prompts.refresh.difflib, "SequenceMatcher", counting)
    diff = strings_diff(
        [_row("a", "x" * 10, ["prose"]), _row("c", "z" * 200, ["prose"])],
        [_row("b", "y" * 10, ["prose"]), _row("d", "w" * 200, ["prose"])],
    )

    # only the two length-plausible pairs construct a matcher; the gate makes
    # the cross-length pairs free
    assert len(built) == 2
    assert diff["added"] and diff["removed"]


def test_refresh_skips_pairing_when_the_strings_are_unchanged(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "2.1.240")
    monkeypatch.setattr(
        "cc_prompts.refresh.fetch_changelog",
        lambda url: "## 2.1.242\n## 2.1.241\n",
    )
    binaries = {
        "2.1.241": build_binary(tmp_path / "b241.bin"),
        "2.1.242": build_binary(tmp_path / "b242.bin", renamed_identifier=True),
    }
    stub_install(monkeypatch, binaries)
    stub_capture(monkeypatch, {})
    calls = []
    monkeypatch.setattr(
        "cc_prompts.refresh.strings_diff",
        lambda prev, cur: calls.append((prev, cur)) or {},
    )

    body = refresh(repo, tmp_path / "cache")

    assert calls == []
    assert "no harness-string changes." in body


def test_strings_diff_skips_pairing_past_its_budget(monkeypatch):
    monkeypatch.setattr("cc_prompts.refresh.PAIRING_MAX_PAIRS", 0)
    old = _row("a", "Short old description line.", ["description"])
    new = _row("b", "Small new description line.", ["description"])

    diff = strings_diff([old], [new])

    assert diff["changed"] == []
    assert diff["added"] == [new]
    assert diff["removed"] == [old]
    section = "\n".join(_render_strings("2.1.242", diff))
    assert "pairing skipped" in section


def test_rows_from_jsonl_hardens_a_hand_edited_baseline():
    line = json.dumps({"key": "k", "text": "T", "rules": "", "occurs": 1})

    assert _rows_from_jsonl(line + "\n \n") == [{"key": "k", "text": "T", "rules": [], "occurs": 1}]
    with pytest.raises(RuntimeError, match="duplicate"):
        _rows_from_jsonl(line + "\n" + line + "\n")
    with pytest.raises(RuntimeError, match="non-string rules"):
        _rows_from_jsonl(
            json.dumps({"key": "k", "text": "L", "rules": ["hook"], "occurs": 1}) + "\n"
        )


def test_report_caps_the_body_and_drops_whole_release_sections():
    line = "x" * 100
    diffs = {"opus.md": ([f"-{line}{i}" for i in range(600)], 600, 600)}

    body = _report("2.1.240", [("2.1.241", diffs, None), ("2.1.242", diffs, None)])

    assert "### 2.1.241" in body
    assert "### 2.1.242" not in body
    assert "the PR body budget is spent" in body
    assert len(body) <= MAX_REPORT_CHARS + 5000


def test_rows_round_trip_through_the_jsonl_file(tmp_path):
    binary = tmp_path / "u2028.bin"
    source = 'var d={description:"alpha beta"};' + ";var pad=1;" * 16
    binary.write_bytes(b"JUNK" + b"// @bun @bytecode" + source.encode() + b"\x00" + b"JUNK")

    rows = extract(binary)

    assert _rows_from_jsonl(render_jsonl(rows)) == rows
