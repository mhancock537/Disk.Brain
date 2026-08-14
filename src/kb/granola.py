"""Granola meeting import.

Meetings become markdown files in a directory that is a normal scan root, so
extraction, enrichment, bundling, indexing and the graph all handle them
through the paths that already exist. No parallel pipeline, no new storage, no
schema change. A meeting is just another document that happens to be a
transcript.

The fetch is deliberately not here. Granola has no documented public API, and
the MCP connector that does work is session-scoped and cannot run inside a
scheduled job. So this takes a file: the export is manual, the ingest is not.

State of the data as of 2026-08-10: the connected account returns zero meetings
because its tier serves only the last 30 days and holds none in that window,
and the older local cache is encrypted. This code imports nothing today and
will import everything the moment that changes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import Config, get_logger

log = get_logger("granola")

SLUG_STRIP = re.compile(r"[^\w\s-]")
SLUG_SPACE = re.compile(r"[\s_-]+")


def slugify(title: str) -> str:
    """A stable filename from a meeting title.

    Stable matters more than pretty: the filename is the identity used to
    decide whether a meeting is new, unchanged or edited, and a title that
    round-trips to a different slug would import the same meeting twice.
    """
    cleaned = SLUG_STRIP.sub(" ", str(title or "")).strip().casefold()
    slug = SLUG_SPACE.sub("-", cleaned).strip("-")
    return slug or "untitled"


def _date_prefix(meeting: dict) -> str:
    """`YYYY-MM-DD-` when the meeting has a usable date, else empty.

    Sorting a folder of meetings by name should sort them by when they
    happened, which is how anyone actually looks for one.
    """
    stamp = str(meeting.get("created_at") or meeting.get("date") or "")
    return f"{stamp[:10]}-" if len(stamp) >= 10 and stamp[4] == "-" else ""


def meeting_filename(meeting: dict) -> str:
    return f"{_date_prefix(meeting)}{slugify(meeting.get('title'))}.md"


def meeting_markdown(meeting: dict) -> str:
    """One meeting as markdown.

    Deliberately plain. The enrichment pass reads this to write the concept, so
    it needs to be legible to a small local model, not clever. Sections that
    have no content are omitted rather than left empty, because an empty
    heading is noise that survives into the chunker.
    """
    title = str(meeting.get("title") or "Untitled meeting").strip()
    parts: list[str] = [f"# {title}", ""]

    stamp = str(meeting.get("created_at") or meeting.get("date") or "")
    facts: list[str] = []
    if stamp:
        facts.append(f"**Date:** {stamp[:10]}")
    attendees = [str(a) for a in (meeting.get("attendees") or []) if a]
    if attendees:
        facts.append(f"**Attendees:** {', '.join(attendees)}")
    duration = meeting.get("duration_minutes")
    if duration:
        facts.append(f"**Duration:** {duration} minutes")
    facts.append("**Source:** Granola")
    parts += facts + [""]

    summary = str(meeting.get("summary") or "").strip()
    if summary:
        parts += ["## Summary", "", summary, ""]

    notes = str(meeting.get("notes") or "").strip()
    if notes:
        parts += ["## Notes", "", notes, ""]

    turns = meeting.get("transcript") or []
    if turns:
        parts += ["## Transcript", ""]
        for turn in turns:
            if isinstance(turn, dict):
                speaker = str(turn.get("speaker") or "Unknown").strip()
                text = str(turn.get("text") or "").strip()
            else:
                speaker, text = "Unknown", str(turn).strip()
            if text:
                parts.append(f"**{speaker}:** {text}")
                parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def _has_content(meeting: dict) -> bool:
    """A title and nothing else is a calendar entry, not a meeting record."""
    return bool(
        str(meeting.get("summary") or "").strip()
        or str(meeting.get("notes") or "").strip()
        or (meeting.get("transcript") or [])
    )


def _meetings_from(payload: Any) -> list[dict]:
    """Accept either a bare list or the connector's `{"meetings": [...]}`."""
    if isinstance(payload, dict):
        payload = payload.get("meetings") or payload.get("results") or []
    if not isinstance(payload, list):
        return []
    return [m for m in payload if isinstance(m, dict)]


def import_meetings(cfg: Config, export_path: Path) -> dict:
    """Write each meeting to `[granola] notes_dir`. Returns counts.

    Idempotent by content: an unchanged meeting is left alone rather than
    rewritten, because rewriting changes the file's mtime and hash and would
    send an unchanged transcript back through an eleven-hour enrichment queue
    for nothing.
    """
    export_path = Path(export_path)
    if not export_path.is_file():
        raise FileNotFoundError(f"no export at {export_path}")

    try:
        payload = json.loads(export_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{export_path} is not valid JSON: {exc}") from exc

    meetings = _meetings_from(payload)
    out_dir = cfg.granola_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    counts = {"imported": 0, "updated": 0, "unchanged": 0, "skipped": 0}
    for meeting in meetings:
        if not _has_content(meeting):
            counts["skipped"] += 1
            log.info("skipped, no content: %s", meeting.get("title"))
            continue

        target = out_dir / meeting_filename(meeting)
        body = meeting_markdown(meeting)

        if target.is_file():
            if target.read_text(encoding="utf-8") == body:
                counts["unchanged"] += 1
                continue
            target.write_text(body, encoding="utf-8")
            counts["updated"] += 1
            log.info("updated %s", target.name)
            continue

        target.write_text(body, encoding="utf-8")
        counts["imported"] += 1
        log.info("imported %s", target.name)

    counts["total_in_export"] = len(meetings)
    counts["notes_dir"] = str(out_dir)
    return counts
