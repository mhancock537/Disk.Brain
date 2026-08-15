# Obsidian Project Cockpit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit Disk.Brain cockpit command that scaffolds an Obsidian vault, captures compact AI session cards, and lets the existing indexing pipeline find those cards.

**Architecture:** A new `kb.cockpit` module owns card validation, vault confinement, Markdown rendering, scaffold creation, and idempotent atomic writes. A nested `kb cockpit` Typer command exposes `init` and `capture` without loading the corpus configuration. Codex reflection calls the capture command after the existing lesson log and candidate-ledger writes succeed. Obsidian, not Disk.Brain, remains the editable project surface.

**Tech Stack:** Python 3.12, standard library `json` and `datetime`, existing Typer CLI, existing `kb.okf.write_atomic`, pytest, Markdown with YAML frontmatter.

**Spec:** `docs/superpowers/specs/2026-08-14-obsidian-project-cockpit-design.md`

## Global Constraints

- Python remains `>=3.12,<3.13`. Add no dependency.
- Session-card JSON input is capped at `262,144` bytes.
- Required scalar fields are `id`, `project`, `occurred_at`, `tool`, `source_ref`, `status`, and `summary`.
- `status` accepts only `completed`, `paused`, or `blocked`.
- List fields are `decisions`, `artifacts`, `open_loops`, and `lesson_keys`. Each member is a non-empty string, and duplicate members are rejected.
- Unknown JSON keys fail validation.
- Capture writes only under the resolved vault’s `Projects/`, `Sessions/`, and `Templates/` directories.
- Existing vault notes, source files, lesson logs, and candidate-ledger entries are never changed by Disk.Brain.
- Repeated capture of identical content succeeds without writing. A different card at the same path fails without overwriting.
- All new files use sibling temporary files and atomic replacement.
- The release captures summaries and provenance, never raw transcripts or cloud data.
- Run the focused tests after every task and the complete suite before handoff.

---

### Task 1: Build the session-card domain module

**Files:**
- Create: `src/kb/cockpit.py`
- Create: `tests/test_cockpit.py`

**Interfaces:**
- Consumes: raw JSON-like mappings, a vault `Path`, and the existing `kb.okf.write_atomic` helper.
- Produces: `SessionCard`, `CaptureResult`, `validate_card(raw)`, `load_card(path)`, `card_path(vault, card)`, `render_session_card(card)`, `init_vault(vault, project_slug="disk-brain")`, and `capture_card(vault, card)`.

- [ ] **Step 1: Write validation tests first.**

  Add tests for a valid card, each missing required scalar, each invalid status, blank strings, duplicate list members, an unknown key, a non-list field, an invalid ISO timestamp, and an input file larger than `262,144` bytes. Assert errors identify the field and reason.

- [ ] **Step 2: Run the focused tests and confirm the module is absent.**

  Run: `.venv/bin/python -m pytest tests/test_cockpit.py -q`

  Expected: FAIL during collection with the missing `kb.cockpit` import.

- [ ] **Step 3: Implement the immutable card types, JSON loader, and validator.**

  Implement `load_card(path)` with a byte-count check before reading, `json.loads`, and `validate_card`. Parse `occurred_at` with `datetime.fromisoformat`, require timezone information, copy lists into tuples, reject unknown keys before constructing `SessionCard`, and preserve the original scalar values. Keep error messages deterministic so CLI tests can assert them.

- [ ] **Step 4: Write path and rendering tests.**

  Assert that a card at `2026-08-14T14:30:00-05:00` and id `diskbrain-cockpit-plan` renders to `Sessions/2026-08/2026-08-14-1430-diskbrain-cockpit-plan.md`, that the path stays below the resolved vault, and that the Markdown contains frontmatter plus the five named body sections: Outcome, Decisions, Artifacts, Open Loops, and Lesson Keys.

- [ ] **Step 5: Implement path confinement and Markdown rendering.**

  Require a safe lowercase id slug matching `[a-z0-9][a-z0-9-]{0,119}`, derive the month directory from the card timestamp, resolve the final path, and verify it is relative to the resolved vault. Render artifact paths as literal code where they are outside the vault. Render the project as `[[Projects/disk-brain]]` and lesson keys as plain values so the card remains useful even when lesson logs live outside the vault.

- [ ] **Step 6: Write scaffold, idempotency, and conflict tests.**

  Assert that `init_vault` creates `Projects/`, `Sessions/`, `Templates/`, `Projects/disk-brain.md`, and `Templates/Session Card.md`, leaves pre-existing files byte-identical, and returns only created paths. Assert that the first capture writes one file, an identical repeat reports `repeated=True` without changing its mtime or bytes, and a different card with the same path raises a conflict without changing the file.

- [ ] **Step 7: Implement scaffold and capture writes.**

  Create missing directories and starter files only. For capture, reject a missing vault, render once, compare an existing file before writing, and call `write_atomic` only for a new file. Return a small `CaptureResult` with `path`, `wrote`, and `repeated` so the CLI does not inspect filesystem state itself.

- [ ] **Step 8: Run the focused tests and commit.**

  Run: `.venv/bin/python -m pytest tests/test_cockpit.py -q`

  Expected: PASS. Commit with:

  ```bash
  git add src/kb/cockpit.py tests/test_cockpit.py
  git commit -m "feat: add Obsidian session card domain"
  ```

### Task 2: Expose `kb cockpit init` and `kb cockpit capture`

**Files:**
- Modify: `src/kb/cli.py`
- Modify: `tests/test_cockpit.py`

**Interfaces:**
- Consumes: the Task 1 domain functions.
- Produces: `kb cockpit init --vault PATH` and `kb cockpit capture --vault PATH --input CARD.json`.

- [ ] **Step 1: Add CLI failure and success tests.**

  Use `typer.testing.CliRunner` to cover valid init, valid capture, missing vault, missing input, invalid JSON, oversized JSON, repeat capture, and conflict capture. Assert exit codes, the created path, and the absence of a traceback. Keep the command independent of `config.toml` so a blank vault can be initialized before corpus setup.

- [ ] **Step 2: Run the CLI tests and confirm the commands are absent.**

  Run: `.venv/bin/python -m pytest tests/test_cockpit.py -q`

  Expected: FAIL for command lookup or import until the nested Typer app exists.

- [ ] **Step 3: Add a nested Typer cockpit app.**

  Declare `cockpit_app = typer.Typer(...)` beside the existing root app and register it with `app.add_typer(cockpit_app, name="cockpit")`. Keep `--vault` required on both subcommands. Keep `--input` a `Path` argument on capture. Do not call `_load`, since these commands do not need the corpus config. Keep the first release on the fixed `disk-brain` project scaffold from the spec.

- [ ] **Step 4: Implement command output and error boundaries.**

  `init` prints the vault and created paths. `capture` prints JSON containing `path`, `wrote`, and `repeated`. Convert validation, JSON decode, missing path, and conflict errors into one-line red messages with exit code 1. A repeat exits 0 and states that the existing card already matches.

- [ ] **Step 5: Run focused and CLI tests, then commit.**

  Run: `.venv/bin/python -m pytest tests/test_cockpit.py -q`

  Expected: PASS. Commit with:

  ```bash
  git add src/kb/cli.py tests/test_cockpit.py
  git commit -m "feat: expose Obsidian cockpit commands"
  ```

### Task 3: Add the Codex reflection adapter

**Files:**
- Modify outside the Disk.Brain repository: `/Users/mike/.agents/skills/session-reflection/SKILL.md`
- Create in the repository: `docs/obsidian-session-card-prompt.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: the existing reflection task outcome, lesson key, artifact paths, and an explicitly configured vault path.
- Produces: one JSON card passed to `kb cockpit capture` after the lesson log and candidate ledger succeed.

- [ ] **Step 1: Write the adapter contract in project documentation.**

  Document that the reflection skill checks an opt-in cockpit profile at `~/.config/diskbrain/cockpit.toml` with:

  ```toml
  [cockpit]
  enabled = true
  vault = "/absolute/path/to/vault"
  ```

  The documentation must state that an absent profile disables capture, a task with no artifact, decision, or lesson key creates no card, and capture errors remain visible without changing reflection status.

- [ ] **Step 2: Add reusable prompts for Claude and ChatGPT.**

  In `docs/obsidian-session-card-prompt.md`, provide one prompt that emits only the approved JSON contract. It must forbid transcript dumps, invented artifact paths, and promotion decisions. Include one valid example and one blocked-task example.

- [ ] **Step 3: Update the Codex reflection skill after canonical logging.**

  Add the cockpit step after the dated lesson log and candidate-ledger upsert. Read the profile, build a card only from observed task facts, write a temporary JSON file under `/private/tmp`, run `kb cockpit capture --vault <profile vault> --input <temp file>`, report the command result, and remove the temporary file. The skill must skip capture when the profile is absent or disabled. It must never write to the lesson log through the cockpit path.

- [ ] **Step 4: Add setup and rollback instructions to the README.**

  Document the one-time vault setup, the profile file, the required `[[scan.roots]]` entry for the vault, and the normal `kb scan`, `kb drain`, `kb index`, and `kb graph` sequence. State that removing or disabling the profile stops automatic cards without touching prior cards.

- [ ] **Step 5: Review the external edit boundary before applying it.**

  The skill file is outside the repository and is not part of the Disk.Brain Git commit. Apply that change only in the user’s local configuration after the core CLI passes. If the environment blocks that write, leave the documentation and manual capture path intact and report the exact handoff.

- [ ] **Step 6: Commit the repository documentation.**

  Run: `rg -n "T.BD|T.DO" docs/obsidian-session-card-prompt.md README.md`

  Run: `rg -n "\\u2014" docs/obsidian-session-card-prompt.md README.md`

  Expected: no placeholder or prohibited punctuation matches. Commit with:

  ```bash
  git add docs/obsidian-session-card-prompt.md README.md
  git commit -m "docs: define Obsidian cockpit reflection capture"
  ```

### Task 4: Verify indexing and the project-cockpit workflow

**Files:**
- Modify: `tests/test_pipeline.py`
- Modify: `tests/test_watch.py`

**Interfaces:**
- Consumes: the Task 2 CLI, the existing scan and drain pipeline, and a temporary vault fixture.
- Produces: automated proof that a captured card becomes searchable without changing existing reflection records.

- [ ] **Step 1: Add a pipeline fixture for a temporary Obsidian vault.**

  Create a vault under pytest’s `tmp_path`, run `kb cockpit init`, capture a card containing a distinctive project phrase and artifact path, and add the vault as the only configured scan root for the test. Keep model calls stubbed through the existing test seams.

- [ ] **Step 2: Add the end-to-end assertions.**

  Run the normal scan, extract, enrich, bundle, index, and graph steps used by the fixture. Assert that a search for the project phrase returns the captured card, that the rendered note contains the source reference, and that a pre-existing lesson file outside the vault remains byte-identical.

- [ ] **Step 3: Add watcher behavior coverage.**

  Capture a second card, run the existing watcher and bounded drain, and assert that the new card enters the manifest without duplicate records. Assert that a repeated capture does not create a second manifest row.

- [ ] **Step 4: Run the full verification suite.**

  Run: `.venv/bin/python -m pytest tests/ -q`

  Expected: PASS with no model downloads and no writes outside pytest temporary directories.

- [ ] **Step 5: Perform the live acceptance check.**

  Run `kb cockpit init --vault <empty-vault>`, open that vault in Obsidian, capture one real Codex task card, add the vault to a copy of `config.toml`, then run the normal scan and drain path. Search by project, decision, and artifact. Confirm the note appears in Obsidian and Disk.Brain, and confirm the dated lesson log and candidate ledger remain the authority for reflection.

- [ ] **Step 6: Commit the verification changes.**

  ```bash
  git add tests/test_pipeline.py tests/test_watch.py README.md
  git commit -m "test: verify Obsidian cockpit indexing"
  ```

## Handoff

After the plan is approved, execute it task by task. The first three repository commits should be independently reviewable. Apply the external Codex skill edit only after the CLI and focused tests pass. The Claude Code, Claude, and ChatGPT integrations remain manual JSON producers until the Codex path proves useful in daily work.
