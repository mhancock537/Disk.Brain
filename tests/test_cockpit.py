"""Obsidian project cockpit cards and vault writes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kb.cli import app
from kb.cockpit import (
    MAX_CARD_BYTES,
    capture_card,
    card_path,
    init_vault,
    load_card,
    render_session_card,
    validate_card,
)


def valid_raw(**overrides) -> dict:
    raw = {
        "id": "diskbrain-cockpit-plan",
        "project": "disk-brain",
        "occurred_at": "2026-08-14T14:30:00-05:00",
        "tool": "codex",
        "source_ref": "Codex task: Disk.Brain project cockpit",
        "status": "completed",
        "summary": "Created the reviewed implementation plan.",
        "decisions": ["Keep lesson records authoritative."],
        "artifacts": ["/Users/mike/Projects/Disk.Brain/README.md"],
        "open_loops": [],
        "lesson_keys": ["keep-canonical-lesson-records"],
    }
    raw.update(overrides)
    return raw


def write_json(path: Path, raw: dict) -> Path:
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_load_card_accepts_the_complete_contract(tmp_path):
    card = load_card(write_json(tmp_path / "card.json", valid_raw()))

    assert card.id == "diskbrain-cockpit-plan"
    assert card.project == "disk-brain"
    assert card.decisions == ("Keep lesson records authoritative.",)
    assert card.open_loops == ()


@pytest.mark.parametrize(
    "field",
    ["id", "project", "occurred_at", "tool", "source_ref", "status", "summary"],
)
def test_validate_card_rejects_each_missing_scalar(field):
    raw = valid_raw()
    raw.pop(field)

    with pytest.raises(ValueError, match=field):
        validate_card(raw)


@pytest.mark.parametrize("status", ["done", "active", "", 4])
def test_validate_card_rejects_an_invalid_status(status):
    with pytest.raises(ValueError, match="status"):
        validate_card(valid_raw(status=status))


@pytest.mark.parametrize("field", ["id", "project", "tool", "source_ref", "summary"])
def test_validate_card_rejects_blank_required_text(field):
    with pytest.raises(ValueError, match=field):
        validate_card(valid_raw(**{field: "  "}))


def test_validate_card_rejects_unknown_fields():
    with pytest.raises(ValueError, match="unknown"):
        validate_card(valid_raw(transcript="raw chat"))


@pytest.mark.parametrize("field", ["decisions", "artifacts", "open_loops", "lesson_keys"])
def test_validate_card_rejects_a_non_list_collection(field):
    with pytest.raises(ValueError, match=field):
        validate_card(valid_raw(**{field: "not a list"}))


@pytest.mark.parametrize("bad_item", ["", "  ", 7, None])
def test_validate_card_rejects_blank_or_non_string_list_members(bad_item):
    with pytest.raises(ValueError, match="decisions"):
        validate_card(valid_raw(decisions=[bad_item]))


def test_validate_card_rejects_duplicate_list_members():
    with pytest.raises(ValueError, match="duplicate"):
        validate_card(valid_raw(artifacts=["one.md", "one.md"]))


@pytest.mark.parametrize("stamp", ["yesterday", "2026-08-14T14:30:00"])
def test_validate_card_requires_a_timezone_aware_iso_timestamp(stamp):
    with pytest.raises(ValueError, match="occurred_at"):
        validate_card(valid_raw(occurred_at=stamp))


@pytest.mark.parametrize(
    "card_id",
    ["../escape", "DiskBrain", "has spaces", "-leading", "trailing-", "a" * 121],
)
def test_validate_card_rejects_an_unsafe_id(card_id):
    with pytest.raises(ValueError, match="id"):
        validate_card(valid_raw(id=card_id))


def test_load_card_rejects_input_over_the_byte_limit(tmp_path):
    path = tmp_path / "large.json"
    path.write_bytes(b"{" + b"x" * MAX_CARD_BYTES + b"}")

    with pytest.raises(ValueError, match="262144"):
        load_card(path)


def test_card_path_uses_the_local_month_and_timestamp(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    card = validate_card(valid_raw())

    assert card_path(vault, card) == (
        vault
        / "Sessions"
        / "2026-08"
        / "2026-08-14-1430-diskbrain-cockpit-plan.md"
    )


def test_card_path_refuses_a_sessions_symlink_that_escapes_the_vault(tmp_path):
    vault = tmp_path / "vault"
    outside = tmp_path / "outside"
    vault.mkdir()
    outside.mkdir()
    (vault / "Sessions").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="vault"):
        card_path(vault, validate_card(valid_raw()))


def test_render_session_card_contains_frontmatter_and_named_sections():
    text = render_session_card(validate_card(valid_raw()))

    assert "id: diskbrain-cockpit-plan" in text
    assert "occurred_at: '2026-08-14T14:30:00-05:00'" in text
    assert "project: '[[Projects/disk-brain]]'" in text
    assert "# Outcome\n\nCreated the reviewed implementation plan." in text
    assert "# Decisions\n\n- Keep lesson records authoritative." in text
    assert "# Artifacts\n\n- `/Users/mike/Projects/Disk.Brain/README.md`" in text
    assert "# Open Loops\n\n- None." in text
    assert "# Lesson Keys\n\n- keep-canonical-lesson-records" in text


def test_init_vault_creates_only_the_cockpit_scaffold(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()

    created = init_vault(vault)

    assert {path.relative_to(vault).as_posix() for path in created} == {
        "Projects",
        "Projects/disk-brain.md",
        "Sessions",
        "Templates",
        "Templates/Session Card.md",
    }
    assert (vault / "Projects/disk-brain.md").read_text().startswith("---\n")
    assert "# Session Card" in (vault / "Templates/Session Card.md").read_text()


def test_init_vault_preserves_existing_files_byte_for_byte(tmp_path):
    vault = tmp_path / "vault"
    project = vault / "Projects" / "disk-brain.md"
    project.parent.mkdir(parents=True)
    project.write_bytes(b"Mike's existing project note\n")

    created = init_vault(vault)

    assert project.read_bytes() == b"Mike's existing project note\n"
    assert project not in created


def test_capture_is_idempotent_without_replacing_the_existing_file(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    init_vault(vault)
    card = validate_card(valid_raw())

    first = capture_card(vault, card)
    before = first.path.stat()
    second = capture_card(vault, card)
    after = first.path.stat()

    assert first.wrote is True
    assert first.repeated is False
    assert second.wrote is False
    assert second.repeated is True
    assert before.st_ino == after.st_ino
    assert before.st_mtime_ns == after.st_mtime_ns


def test_capture_refuses_to_overwrite_different_content_at_the_same_path(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    init_vault(vault)
    first = capture_card(vault, validate_card(valid_raw()))
    before = first.path.read_bytes()

    changed = validate_card(valid_raw(summary="A different outcome."))
    with pytest.raises(FileExistsError, match="different content"):
        capture_card(vault, changed)

    assert first.path.read_bytes() == before


def test_capture_requires_an_existing_vault(tmp_path):
    with pytest.raises(FileNotFoundError, match="vault"):
        capture_card(tmp_path / "missing", validate_card(valid_raw()))


# --- CLI --------------------------------------------------------------------


def test_cockpit_init_creates_a_vault_scaffold(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()

    result = CliRunner().invoke(app, ["cockpit", "init", "--vault", str(vault)])

    assert result.exit_code == 0, result.output
    assert "Projects/disk-brain.md" in result.output
    assert (vault / "Templates/Session Card.md").is_file()


def test_cockpit_init_reports_a_missing_vault_without_a_traceback(tmp_path):
    result = CliRunner().invoke(
        app, ["cockpit", "init", "--vault", str(tmp_path / "missing")]
    )

    assert result.exit_code == 1
    assert "vault directory does not exist" in result.output
    assert "Traceback" not in result.output


def test_cockpit_capture_writes_a_valid_card(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    init_vault(vault)
    source = write_json(tmp_path / "card.json", valid_raw())

    result = CliRunner().invoke(
        app,
        ["cockpit", "capture", "--vault", str(vault), "--input", str(source)],
    )

    assert result.exit_code == 0, result.output
    assert '"wrote": true' in result.output
    assert '"repeated": false' in result.output
    assert (vault / "Sessions/2026-08/2026-08-14-1430-diskbrain-cockpit-plan.md").is_file()


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("missing.json", None, "does not exist"),
        ("broken.json", b"{not json", "not valid JSON"),
        ("large.json", b"{" + b"x" * MAX_CARD_BYTES + b"}", "262144"),
    ],
    ids=["missing", "broken", "large"],
)
def test_cockpit_capture_reports_bad_input_without_a_traceback(
    tmp_path, filename, content, message
):
    vault = tmp_path / "vault"
    vault.mkdir()
    source = tmp_path / filename
    if content is not None:
        source.write_bytes(content)

    result = CliRunner().invoke(
        app,
        ["cockpit", "capture", "--vault", str(vault), "--input", str(source)],
    )

    assert result.exit_code == 1
    assert message in result.output
    assert "Traceback" not in result.output


def test_cockpit_capture_reports_an_identical_repeat_as_success(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    source = write_json(tmp_path / "card.json", valid_raw())
    runner = CliRunner()

    first = runner.invoke(
        app,
        ["cockpit", "capture", "--vault", str(vault), "--input", str(source)],
    )
    second = runner.invoke(
        app,
        ["cockpit", "capture", "--vault", str(vault), "--input", str(source)],
    )

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert '"wrote": false' in second.output
    assert '"repeated": true' in second.output


def test_cockpit_capture_reports_a_conflict_without_overwriting(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    source = write_json(tmp_path / "card.json", valid_raw())
    runner = CliRunner()
    first = runner.invoke(
        app,
        ["cockpit", "capture", "--vault", str(vault), "--input", str(source)],
    )
    note = vault / "Sessions/2026-08/2026-08-14-1430-diskbrain-cockpit-plan.md"
    before = note.read_bytes()
    write_json(source, valid_raw(summary="Changed after capture."))

    second = runner.invoke(
        app,
        ["cockpit", "capture", "--vault", str(vault), "--input", str(source)],
    )

    assert first.exit_code == 0, first.output
    assert second.exit_code == 1
    assert "different content" in second.output
    assert "Traceback" not in second.output
    assert note.read_bytes() == before
