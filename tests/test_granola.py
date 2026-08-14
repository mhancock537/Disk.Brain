"""Granola meeting import: export file to markdown the pipeline already handles."""

from __future__ import annotations

import json

import pytest

from kb.granola import import_meetings, meeting_markdown, slugify


def _meeting(**over):
    m = {
        "id": "6f1c2d3e-0000-4000-8000-000000000001",
        "title": "Quarterly pricing with Dana",
        "created_at": "2026-08-04T15:30:00Z",
        "attendees": ["Ada Lovelace", "Dana Reyes"],
        "summary": "Agreed to keep the widget list price unchanged for now.",
        "notes": "Dana argued for a discount. Held.",
        "transcript": [
            {"speaker": "Me", "text": "We are holding at list."},
            {"speaker": "Dana Reyes", "text": "A discount is what the market pays."},
        ],
    }
    m.update(over)
    return m


def test_slugify_makes_a_stable_filename():
    assert slugify("Quarterly pricing with Dana") == "quarterly-pricing-with-dana"
    assert slugify("  Multiple   spaces & symbols!  ") == "multiple-spaces-symbols"
    assert slugify("") == "untitled"


def test_markdown_carries_the_title_date_and_attendees():
    md = meeting_markdown(_meeting())
    assert "# Quarterly pricing with Dana" in md
    assert "2026-08-04" in md
    assert "Ada Lovelace" in md and "Dana Reyes" in md


def test_markdown_labels_every_speaker_turn():
    """Speaker labels are the whole point of a transcript. Losing them makes it
    an undifferentiated wall of text that no one can attribute."""
    md = meeting_markdown(_meeting())
    assert "**Me:** We are holding at list." in md
    assert "**Dana Reyes:** A discount is what the market pays." in md


def test_markdown_survives_a_meeting_with_no_transcript():
    md = meeting_markdown(_meeting(transcript=[]))
    assert "# Quarterly pricing with Dana" in md
    assert "Transcript" not in md, "an empty section is noise"


def test_markdown_survives_missing_optional_fields():
    md = meeting_markdown({"id": "x", "title": "Bare"})
    assert "# Bare" in md


def test_import_writes_one_file_per_meeting(cfg, tmp_path):
    export = tmp_path / "export.json"
    export.write_text(json.dumps([_meeting(), _meeting(id="b", title="Second call")]))

    result = import_meetings(cfg, export)

    assert result["imported"] == 2
    written = sorted(p.name for p in cfg.granola_dir.glob("*.md"))
    assert written == [
        "2026-08-04-quarterly-pricing-with-dana.md",
        "2026-08-04-second-call.md",
    ]


def test_a_meeting_with_no_date_still_gets_a_filename(cfg, tmp_path):
    """Sorting by name should sort by date, but a dateless meeting must not
    lose its file over it."""
    export = tmp_path / "export.json"
    m = _meeting()
    m.pop("created_at")
    export.write_text(json.dumps([m]))

    assert import_meetings(cfg, export)["imported"] == 1
    assert [p.name for p in cfg.granola_dir.glob("*.md")] == [
        "quarterly-pricing-with-dana.md"
    ]


def test_import_is_idempotent(cfg, tmp_path):
    """Re-importing the same export must not duplicate meetings.

    The whole corpus is keyed on file hashes, so a second copy of one meeting
    under a second name would become a second concept saying the same thing.
    """
    export = tmp_path / "export.json"
    export.write_text(json.dumps([_meeting()]))

    first = import_meetings(cfg, export)
    second = import_meetings(cfg, export)

    assert first["imported"] == 1
    assert second["imported"] == 0
    assert second["unchanged"] == 1
    assert len(list(cfg.granola_dir.glob("*.md"))) == 1


def test_import_rewrites_a_meeting_whose_content_changed(cfg, tmp_path):
    export = tmp_path / "export.json"
    export.write_text(json.dumps([_meeting()]))
    import_meetings(cfg, export)

    export.write_text(json.dumps([_meeting(summary="Actually agreed 35k.")]))
    result = import_meetings(cfg, export)

    assert result["updated"] == 1
    assert "35k" in next(cfg.granola_dir.glob("*.md")).read_text()


def test_import_accepts_the_connector_envelope_shape(cfg, tmp_path):
    """The MCP connector returns {"meetings": [...]}, not a bare list."""
    export = tmp_path / "export.json"
    export.write_text(json.dumps({"count": 1, "meetings": [_meeting()]}))
    assert import_meetings(cfg, export)["imported"] == 1


def test_import_refuses_a_file_that_is_not_json(cfg, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json {{{")
    with pytest.raises(ValueError, match="not valid JSON"):
        import_meetings(cfg, bad)


def test_import_refuses_a_missing_file(cfg, tmp_path):
    with pytest.raises(FileNotFoundError):
        import_meetings(cfg, tmp_path / "nope.json")


def test_import_skips_an_entry_with_no_usable_content(cfg, tmp_path):
    """A meeting with a title and nothing else is not worth a concept."""
    export = tmp_path / "export.json"
    export.write_text(json.dumps([{"id": "empty", "title": "Held but not recorded"}]))
    result = import_meetings(cfg, export)
    assert result["imported"] == 0
    assert result["skipped"] == 1


def test_the_default_notes_dir_never_escapes_the_repo(cfg):
    """Written after the first run created files in the real ~/granola-notes.

    A Config with no [granola] section is what every test fixture and every
    fresh checkout has. If the default is absolute, that writes into the user's
    home. The default is relative and resolves against root_dir, so the blast
    radius of a missing config section is the repo, not $HOME.
    """
    from pathlib import Path

    assert cfg.granola_dir.is_relative_to(cfg.root_dir)
    assert not cfg.granola_dir.is_relative_to(Path.home() / "granola-notes")


def test_an_absolute_notes_dir_is_honoured(cfg, tmp_path):
    """Relative-by-default must not mean relative-only."""
    from dataclasses import replace

    elsewhere = tmp_path / "somewhere-else"
    c = replace(cfg, raw={"granola": {"notes_dir": str(elsewhere)}})
    assert c.granola_dir == elsewhere
