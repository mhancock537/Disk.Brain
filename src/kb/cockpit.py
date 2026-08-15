"""Obsidian project cockpit scaffolding and session-card capture."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .okf import Document, render, write_atomic

MAX_CARD_BYTES = 262_144

SCALAR_FIELDS = (
    "id",
    "project",
    "occurred_at",
    "tool",
    "source_ref",
    "status",
    "summary",
)
LIST_FIELDS = ("decisions", "artifacts", "open_loops", "lesson_keys")
ALLOWED_FIELDS = frozenset((*SCALAR_FIELDS, *LIST_FIELDS))
STATUSES = frozenset({"completed", "paused", "blocked"})
SAFE_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,118}[a-z0-9])?$")


@dataclass(frozen=True)
class SessionCard:
    id: str
    project: str
    occurred_at: str
    tool: str
    source_ref: str
    status: str
    summary: str
    decisions: tuple[str, ...]
    artifacts: tuple[str, ...]
    open_loops: tuple[str, ...]
    lesson_keys: tuple[str, ...]

    @property
    def occurred(self) -> datetime:
        return datetime.fromisoformat(self.occurred_at)


@dataclass(frozen=True)
class CaptureResult:
    path: Path
    wrote: bool
    repeated: bool


def _required_text(raw: Mapping[str, Any], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _string_list(raw: Mapping[str, Any], field: str) -> tuple[str, ...]:
    value = raw.get(field, [])
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")

    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field} members must be non-empty strings")
        cleaned.append(item.strip())
    if len(set(cleaned)) != len(cleaned):
        raise ValueError(f"{field} contains a duplicate member")
    return tuple(cleaned)


def _validate_slug(value: str, field: str) -> None:
    if not SAFE_SLUG.fullmatch(value):
        raise ValueError(
            f"{field} must be a lowercase slug of 1 to 120 letters, numbers, or hyphens"
        )


def validate_card(raw: object) -> SessionCard:
    """Validate one JSON object and return an immutable session card."""
    if not isinstance(raw, Mapping):
        raise ValueError("card must be a JSON object")

    unknown = sorted(set(raw) - ALLOWED_FIELDS)
    if unknown:
        raise ValueError(f"unknown card fields: {', '.join(unknown)}")

    values = {field: _required_text(raw, field) for field in SCALAR_FIELDS}
    _validate_slug(values["id"], "id")
    _validate_slug(values["project"], "project")

    try:
        occurred = datetime.fromisoformat(values["occurred_at"])
    except ValueError as exc:
        raise ValueError("occurred_at must be a timezone-aware ISO 8601 timestamp") from exc
    if occurred.tzinfo is None or occurred.utcoffset() is None:
        raise ValueError("occurred_at must be a timezone-aware ISO 8601 timestamp")

    if values["status"] not in STATUSES:
        raise ValueError("status must be completed, paused, or blocked")

    lists = {field: _string_list(raw, field) for field in LIST_FIELDS}
    return SessionCard(**values, **lists)


def load_card(path: Path) -> SessionCard:
    """Load a bounded JSON file and validate its card contract."""
    size = path.stat().st_size
    if size > MAX_CARD_BYTES:
        raise ValueError(f"card input exceeds the {MAX_CARD_BYTES} byte limit")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"card input is not valid JSON: {exc.msg}") from exc
    return validate_card(raw)


def _confined(vault: Path, path: Path) -> Path:
    root = vault.expanduser().resolve()
    target = path.expanduser().resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"cockpit path escapes the vault: {target}") from exc
    return target


def card_path(vault: Path, card: SessionCard) -> Path:
    """Return the confined month path for one session card."""
    stamp = card.occurred
    relative = (
        Path("Sessions")
        / stamp.strftime("%Y-%m")
        / f"{stamp.strftime('%Y-%m-%d-%H%M')}-{card.id}.md"
    )
    return _confined(vault, vault / relative)


def _bullets(values: tuple[str, ...], *, code: bool = False) -> str:
    if not values:
        return "- None."
    if code:
        return "\n".join(f"- `{value.replace('`', r'\`')}`" for value in values)
    return "\n".join(f"- {value}" for value in values)


def render_session_card(card: SessionCard) -> str:
    """Render one card as Obsidian-readable Markdown."""
    frontmatter = {
        "id": card.id,
        "project": f"[[Projects/{card.project}]]",
        "occurred_at": card.occurred_at,
        "tool": card.tool,
        "source_ref": card.source_ref,
        "status": card.status,
    }
    body = "\n\n".join(
        (
            f"# Outcome\n\n{card.summary}",
            f"# Decisions\n\n{_bullets(card.decisions)}",
            f"# Artifacts\n\n{_bullets(card.artifacts, code=True)}",
            f"# Open Loops\n\n{_bullets(card.open_loops)}",
            f"# Lesson Keys\n\n{_bullets(card.lesson_keys)}",
        )
    )
    return render(Document(frontmatter=frontmatter, body=body))


def _starter_project(project_slug: str) -> str:
    title = project_slug.replace("-", " ").title()
    return render(
        Document(
            frontmatter={"project": project_slug, "status": "active"},
            body=(
                f"# {title}\n\n"
                "## Current Outcome\n\n"
                "Describe the result this project needs next.\n\n"
                "## Decisions\n\n"
                "- None recorded.\n\n"
                "## Open Loops\n\n"
                "- None recorded."
            ),
        )
    )


SESSION_TEMPLATE = """---
id:
project: disk-brain
occurred_at:
tool:
source_ref:
status: completed
---

# Session Card

## Outcome

## Decisions

## Artifacts

## Open Loops

## Lesson Keys
"""


def init_vault(vault: Path, project_slug: str = "disk-brain") -> list[Path]:
    """Create the small cockpit scaffold without changing existing notes."""
    _validate_slug(project_slug, "project")
    root = vault.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"vault directory does not exist: {root}")

    created: list[Path] = []
    for name in ("Projects", "Sessions", "Templates"):
        directory = _confined(root, root / name)
        if not directory.exists():
            directory.mkdir()
            created.append(directory)
        elif not directory.is_dir():
            raise NotADirectoryError(f"cockpit path is not a directory: {directory}")

    starters = {
        root / "Projects" / f"{project_slug}.md": _starter_project(project_slug),
        root / "Templates" / "Session Card.md": SESSION_TEMPLATE,
    }
    for raw_path, text in starters.items():
        path = _confined(root, raw_path)
        if not path.exists():
            write_atomic(path, text)
            created.append(path)
    return created


def capture_card(vault: Path, card: SessionCard) -> CaptureResult:
    """Write one immutable session note, or report an identical repeat."""
    root = vault.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"vault directory does not exist: {root}")

    path = card_path(root, card)
    text = render_session_card(card)
    if path.exists():
        if not path.is_file():
            raise FileExistsError(f"cockpit path is not a file: {path}")
        if path.read_text(encoding="utf-8") == text:
            return CaptureResult(path=path, wrote=False, repeated=True)
        raise FileExistsError(f"session card already exists with different content: {path}")

    write_atomic(path, text)
    return CaptureResult(path=path, wrote=True, repeated=False)
