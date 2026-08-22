from pathlib import Path

from cc_prompts.normalize import normalize


def build_sample() -> str:
    home = str(Path.home())
    encoded_home = home.lstrip("/").replace("/", "-")
    memory_line = (
        "You have a persistent file-based memory at "
        f"`/mnt/scratch/tmp/tmp6jpvfo1i/projects/-{encoded_home}-repos-py-cc-system-prompts/memory/`."
    )
    return f"""You are an interactive agent.
 - Primary working directory: {home}/repos/py/cc-system-prompts
 - Is a git repository: true
 - OS Version: Linux 7.1.9-zen1-1-zen
You are powered by the model named Opus 5 (1M context). The exact model ID is claude-opus-5[1m].
Assistant knowledge cutoff is May 2026.
Today's date is 2026-08-22.
see session 9a3f8c2e-1b4d-4c5a-9e2f-7d6b1a8c0e3d for context
stray note lives at {home}/notes/ops.md
{memory_line}
"""


def test_line_rules_replace_per_instance_values():
    text = normalize(build_sample())
    assert "Primary working directory: <cwd>" in text
    assert "Is a git repository: <git-repo>" in text
    assert "OS Version: <os-version>" in text
    assert "You are powered by the model named <model>." in text
    assert "The exact model ID is <model-id>." in text
    assert "Assistant knowledge cutoff is <cutoff>." in text
    assert "May 2026" not in text


def test_no_home_path_survives():
    text = normalize(build_sample())
    assert str(Path.home()) not in text
    assert "<home>/notes/ops.md" in text


def test_memory_dir_path_becomes_a_placeholder():
    text = normalize(build_sample())
    assert "`<memory-dir>`" in text
    assert "tmp6jpvfo1i" not in text
    assert "uwuclxdy" not in text
    assert "memory/" not in text


def test_dates_and_session_ids_become_placeholders():
    text = normalize(build_sample())
    assert "2026-08-22" not in text
    assert "<date>" in text
    assert "9a3f8c2e" not in text
    assert "<session-id>" in text


def test_prompt_body_content_is_untouched():
    text = normalize(build_sample())
    assert "You are an interactive agent." in text
