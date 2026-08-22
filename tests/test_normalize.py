from pathlib import Path

from cc_prompts.normalize import normalize


def build_sample() -> str:
    home = str(Path.home())
    encoded_home = home.lstrip("/").replace("/", "-")
    memory_line = (
        "You have a persistent file-based memory at "
        f"`/mnt/scratch/tmp/tmp6jpvfo1i/projects/-{encoded_home}-repos-py-cc-system-prompts/memory/`."
    )
    tokens_line = "<total_tokens>15000000 tokens left</total_tokens>"
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
{tokens_line}
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
    assert "memory/" not in text


def test_dash_encoded_home_is_replaced_outside_a_memory_path():
    """The scratchpad section spells the home dir dash-encoded, and it is not a
    `/memory/` path, so the memory-dir rule never reaches it."""
    encoded = str(Path.home()).lstrip("/").replace("/", "-")
    text = normalize(f"Scratchpad Directory: /mnt/scratch/tmp/-{encoded}-repos-py-thing\n")
    assert encoded not in text
    assert "<home>" in text


def test_bare_account_name_is_replaced():
    """A username with no path around it: every path-shaped rule misses it."""
    # derived, never spelled out. a hardcoded username would defeat the
    # assertion by putting the thing under test back into a published file.
    user = Path.home().name
    text = normalize(f"a bare mention of {user} in ordinary prose\n")
    assert user not in text
    assert "a bare mention of <user> in ordinary prose" in text


def test_dates_and_session_ids_become_placeholders():
    text = normalize(build_sample())
    assert "2026-08-22" not in text
    assert "<date>" in text
    assert "9a3f8c2e" not in text
    assert "<session-id>" in text


def test_prompt_body_content_is_untouched():
    text = normalize(build_sample())
    assert "You are an interactive agent." in text


def test_quota_tokens_become_a_placeholder():
    text = normalize(build_sample())
    assert "15000000" not in text
    assert "<total_tokens><tokens-left> tokens left</total_tokens>" in text


def build_git_status_sample() -> str:
    return """gitStatus: This is the git status at the start of the conversation.

Current branch: some-private-branch

Main branch (you will usually use this for PRs): trunk

Git user: a-real-person

Status:
M src/secret_project/thing.py
?? notes-about-a-client.md

Recent commits:
deadbee fix(auth): patch the customer login bug
cafef00 feat(billing): add the unreleased pricing tier
"""


def test_git_status_block_keeps_its_labels():
    text = normalize(build_git_status_sample())
    assert "gitStatus: This is the git status at the start of the conversation." in text
    assert "Current branch: <branch>" in text
    assert "Main branch (you will usually use this for PRs): <main-branch>" in text
    assert "Git user: <git-user>" in text


def test_git_status_block_leaks_no_per_run_values():
    text = normalize(build_git_status_sample())
    for leaked in (
        "some-private-branch",
        "trunk",
        "a-real-person",
        "secret_project",
        "notes-about-a-client",
        "deadbee",
        "customer login",
        "unreleased pricing",
    ):
        assert leaked not in text, f"{leaked!r} survived normalization"


def test_normalize_is_idempotent():
    once = normalize(build_git_status_sample())
    assert normalize(once) == once
