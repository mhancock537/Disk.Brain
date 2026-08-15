# Obsidian Session Card Prompt

Use this prompt at the end of a useful Claude or ChatGPT session. Save the
response as JSON, then pass it to `kb cockpit capture`.

## Prompt

```text
Create one Disk.Brain session card from this session.

Return only one JSON object. Do not wrap it in a code fence. Do not include the
raw transcript. Use facts shown in this session. Do not invent file paths,
lesson keys, decisions, or a source reference.

Use exactly these fields:

id: A lowercase slug with 1 to 120 letters, numbers, or hyphens. Do not put a
timestamp in the id.
project: A lowercase project slug.
occurred_at: The current ISO 8601 timestamp with its UTC offset.
tool: codex, claude-code, claude, or chatgpt.
source_ref: A real task, thread, or session reference. Use "manual capture"
when no durable reference exists.
status: completed, paused, or blocked.
summary: One concrete sentence stating the result.
decisions: A JSON list of decisions made in this session.
artifacts: A JSON list of real file paths or URLs produced or changed.
open_loops: A JSON list of unfinished work.
lesson_keys: A JSON list of lesson keys recorded by the reflection workflow.

Use empty lists when the session has no value for a list field. Do not make a
lesson key or promotion decision from ordinary chat content.
```

## Completed example

```json
{
  "id": "diskbrain-cockpit-plan",
  "project": "disk-brain",
  "occurred_at": "2026-08-14T14:30:00-05:00",
  "tool": "chatgpt",
  "source_ref": "manual capture",
  "status": "completed",
  "summary": "Created the approved Obsidian project cockpit plan.",
  "decisions": ["Keep lesson logs and the candidate ledger authoritative."],
  "artifacts": ["/Users/mike/Projects/Disk.Brain/docs/superpowers/plans/2026-08-14-obsidian-project-cockpit.md"],
  "open_loops": ["Implement the Codex reflection adapter."],
  "lesson_keys": []
}
```

## Blocked example

```json
{
  "id": "diskbrain-live-vault-check",
  "project": "disk-brain",
  "occurred_at": "2026-08-14T16:10:00-05:00",
  "tool": "claude",
  "source_ref": "manual capture",
  "status": "blocked",
  "summary": "The live vault check stopped since no Obsidian vault path was configured.",
  "decisions": [],
  "artifacts": [],
  "open_loops": ["Create an Obsidian vault and record its absolute path."],
  "lesson_keys": []
}
```
