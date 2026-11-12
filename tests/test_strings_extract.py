import json

import pytest

from cc_prompts.strings_extract import extract, render_jsonl
from fixture_binary import EXPECTED_ROWS, build_binary, row_text_key


def test_extract_rows_match_the_hand_derived_fixture(tmp_path):
    binary = build_binary(tmp_path / "fixture.bin")
    assert extract(binary) == EXPECTED_ROWS


def test_row_keys_derive_from_the_row_text():
    for row in EXPECTED_ROWS:
        assert row["key"] == row_text_key(row["text"])


def test_render_jsonl_is_one_sorted_line_per_row(tmp_path):
    binary = build_binary(tmp_path / "fixture.bin")
    rendered = render_jsonl(extract(binary))
    lines = rendered.split("\n")[:-1]
    assert len(lines) == len(EXPECTED_ROWS)
    assert [json.loads(line)["text"] for line in lines] == [row["text"] for row in EXPECTED_ROWS]
    assert '"rules": "reminder"' in lines[0]
    assert rendered.endswith("\n")


def test_extract_is_byte_identical_across_runs(tmp_path):
    binary = build_binary(tmp_path / "fixture.bin")
    assert render_jsonl(extract(binary)) == render_jsonl(extract(binary))


def test_renamed_identifier_inside_interpolation_keeps_rows(tmp_path):
    base = extract(build_binary(tmp_path / "base.bin"))
    renamed = extract(build_binary(tmp_path / "renamed.bin", renamed_identifier=True))
    assert renamed == base


def test_module_moved_to_another_offset_keeps_rows(tmp_path):
    base = extract(build_binary(tmp_path / "base.bin"))
    moved = extract(build_binary(tmp_path / "moved.bin", extra_head_module=True))
    assert moved == base


def test_bunfs_import_paths_are_dropped(tmp_path):
    binary = build_binary(tmp_path / "fixture.bin")
    assert all(not row["text"].startswith("/$bunfs/root/") for row in extract(binary))


def test_extract_refuses_a_binary_with_no_modules(tmp_path):
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"no bundle segments in here at all")
    with pytest.raises(RuntimeError, match="carved no modules"):
        extract(empty)


def test_extract_refuses_an_empty_binary(tmp_path):
    empty = tmp_path / "zero.bin"
    empty.write_bytes(b"")
    with pytest.raises(RuntimeError, match="carved no modules"):
        extract(empty)


def test_extract_refuses_modules_that_keep_no_strings(tmp_path):
    benign = tmp_path / "benign.bin"
    benign.write_bytes(b"JUNK" + b"// @bun @bytecode" + b";var z=1;" * 30 + b"\x00" + b"JUNK")
    with pytest.raises(RuntimeError, match="kept no strings"):
        extract(benign)
