# Obsidian Project Cockpit Design

## Goal

Create a local Obsidian project cockpit that collects concise AI session cards. Disk.Brain indexes the cockpit with the rest of the Mac. Existing lesson logs and the candidate ledger remain the lesson system of record.

## First Release

- kb cockpit init creates a small folder structure and session-card template in a selected vault.
- kb cockpit capture validates one JSON card and writes one Markdown note.
- Codex task-end reflection writes a card after it writes the current lesson log and candidate-ledger entry.

The release does not parse historical chats, copy raw transcripts, alter source files, edit existing project notes, run in the background, or call cloud APIs.

## Boundaries

The dated lesson log and candidate ledger retain their authority. A cockpit card links to lesson keys and artifact paths. It does not duplicate a full lesson entry or decide promotions.

Disk.Brain remains read-only for scanned source material. The cockpit command is an explicit, opt-in writer for a vault supplied on the command line. It writes only Projects, Sessions, and Templates below the resolved vault path. It never changes a note it did not create.

## Card Contract

The input is one JSON file. It gives Codex, Claude Code, Claude, and ChatGPT one contract without assuming transcript access or a platform API.

~~~json
{
  "id": "diskbrain-cockpit-plan",
  "project": "disk-brain",
  "occurred_at": "2026-08-14T14:30:00-05:00",
  "tool": "codex",
  "source_ref": "Codex task: Disk.Brain project cockpit",
  "status": "completed",
  "summary": "Created the reviewed implementation plan.",
  "decisions": ["Keep lessons and the ledger as the lesson record."],
  "artifacts": ["/Users/mike/Projects/Disk.Brain/README.md"],
  "open_loops": [],
  "lesson_keys": []
}
~~~

Required scalar fields are id, project, occurred_at, tool, source_ref, status, and summary. Status accepts completed, paused, or blocked. The four list fields contain non-empty strings. Unknown keys fail validation.

## Layout

~~~text
<vault>/
  Projects/
    disk-brain.md
  Sessions/
    2026-08/
      2026-08-14-1430-diskbrain-cockpit-plan.md
  Templates/
    Session Card.md
~~~

The init command creates folders and starter files only when absent. Capture writes YAML frontmatter plus Outcome, Decisions, Artifacts, Open Loops, and Lesson Keys. Project notes are manual and stable. Cards link with Obsidian wiki links.

## Safety

The card path uses the timestamp plus an ID slug. The command rejects path escapes, invalid dates, blank required values, duplicate list items, JSON input files larger than **262,144 bytes**, and unknown fields. It writes through a sibling temporary file and atomic replacement.

An identical repeat reports success without a write. A different card at the same path fails. A later agent cannot silently change a completed-work record.

## Reflection Wiring

Reflection writes the lesson log and candidate ledger first. The opt-in cockpit profile lives at `~/.config/diskbrain/cockpit.toml` and has this shape:

~~~toml
[cockpit]
enabled = true
vault = "/absolute/path/to/vault"
~~~

An enabled cockpit profile creates a card from facts already present in the task outcome. A task without an artifact, decision, or lesson key creates no card. Capture errors remain visible and do not change reflection status.

Codex is the first integration. Claude Code adopts the same JSON contract after the Codex path passes real use. Claude and ChatGPT start with a reusable prompt that emits JSON. Their automation is outside this release.

## Verification

- Unit tests cover validation, vault confinement, scaffold behavior, rendering, idempotency, and conflict refusal.
- CLI tests cover valid capture, invalid JSON, missing input, and repeat capture.
- A live check opens the vault in Obsidian, captures one Codex task, then finds it through Disk.Brain after the normal scan and drain path.

## Success Criteria

- One command creates a usable vault scaffold.
- A valid card becomes a readable note in the intended month folder.
- Existing notes and reflection records stay unchanged.
- A repeat never creates duplicates or changes a prior card.
- A manual Claude or ChatGPT card uses the same schema.
- Disk.Brain finds a captured card by project, decision, or artifact.
