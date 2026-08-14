# Roadmap

Direction and sequencing. The task inventory lives in the code and in
`DECISIONS.md`; this file says what is being built and in what order.

## Phases 1 to 7 — local corpus

| Phase | What | State |
| --- | --- | --- |
| 1 | Manifest and extraction | done 2026-08-07 |
| 2 | OKF bundle generation | done 2026-08-07 (code); full enrichment runs 22:00 |
| 3 | Chunk, embed, index | done 2026-08-07 |
| 4 | Graph (LadybugDB) | done 2026-08-07 |
| 5 | Hybrid retrieval | done 2026-08-07 |
| 6 | MCP server | done 2026-08-07 |
| 7 | Incremental updates | done 2026-08-07 |

The single long job is enrichment: 2,403 documents remaining, about 11 hours on
`qwen3:14b-q4_K_M`. It runs detached, overnight, once phases 3 to 7 are code
complete, so the 11 hours are spent against finished code.

## Phase 8 — cloud sources (added 2026-08-07)

Two sources join the knowledge base. Everything else stays out: no Gmail, no
Linear, no HubSpot, no ClickUp, no event log, no entity merging.

### 8A — cloud text mirror — NOT BUILT, dropped 2026-08-09

Verified before writing any code, which is what this section asked for, and the
verification killed it. rclone v1.75.0 installed, `gdrive` configured at
`scope = drive.readonly`, config file already 600, remote reachable.

The survey (`rclone lsjson --recursive --hash gdrive:`) came back with 330
files, of which **244 are JPEGs**. After the image, video and archive filters
this section already specifies, the whole of 8A is **79 documents, 8.1 MB**, a
3% increase on a 2,425-document corpus, in exchange for a manifest table, a
hand-built diff, a staging flow and scheduler wiring. Mike's call: skip it.

Two assumptions below were also wrong, recorded so a future attempt starts from
the truth:

- **Zero Google-native files.** No Docs, Sheets or Slides. The export paragraph
  had nothing to act on, and the hash-diff concern it created does not exist.
- **OneDrive is out**, so half the surface disappears with it.

Sensitivity, if this is ever revisited: `personal` for the whole `gdrive`
remote, decided after finding that work product (a patent strategy document, a
mutual NDA) sits in that personal account.

The original plan follows, unbuilt.

`rclone` with two read-only remotes, `gdrive` and `onedrive`.

- Google Drive scope must be `drive.readonly`. Refuse to proceed if wider.
- `chmod 600 ~/.config/rclone/rclone.conf`, confirmed.
- Native Google formats export as text: Docs to `md`, Sheets to `csv`, Slides to `pptx`.
- Verify rclone version and remote reachability before writing pipeline code.

Diff is built by hand, not by `rclone bisync` or `rclone sync`:

1. `rclone lsjson --recursive --hash` per remote, capturing ID, path, size,
   modtime, MIME, hash.
2. New `cloud_manifest` table: remote, remote_id, path, size, modtime, hash,
   mime, status, concept_id, last_seen_run.
3. Diff against the last run. New and changed rows queue; missing rows mark the
   concept deprecated.
4. `rclone copyto` each queued file into `data/staging/`, one at a time.
5. Extract with the existing Phase 1 extractors.
6. Delete the staged file. Only extracted markdown and the manifest row survive.

Filters: skip over 100 MB, skip video, audio, images, archives, installers, skip
Drive trash and non-allowlisted shared drives. Folder allowlist in `config.toml`.

Sensitivity rules come from `config.toml`. Work-owned accounts default to
`work`. Ambiguous cases are marked `unknown` and listed at the checkpoint, never
guessed.

**Checkpoint 8A**: file counts per remote, extraction success rate, total
extracted words, peak `data/staging/` disk use, full list of `unknown` files.

### 8B — meeting transcripts — Granola only, import path BUILT 2026-08-10

**Fireflies is dropped entirely.** Mike does not use it. Removed 2026-08-10,
not deferred. Everything below about GraphQL, bearer keys and 429 back-off is
gone with it.

**Granola is the meeting source.** Mike uses it. The data situation, verified
2026-08-10 rather than assumed:

| path | result |
| --- | --- |
| connector `list_meetings`, last 30 days | 0 meetings |
| connector `query_granola_meetings` | no notes available |
| connector `list_meeting_folders` | "only available to paid Granola tiers" |
| local `cache-v6.json` | empty state shell, `"transcripts":{}` |
| local `granola.db`, `cache-v6.json.enc` | high-entropy bytes, last written 2026-06-15 |

So there is currently nothing to import. The account is on a tier that serves
only the last 30 days and holds nothing in that window, and the older local
data is encrypted. Decrypting an app's private store was not attempted and will
not be.

**What is built anyway**, because the blocker is a plan tier rather than a
design problem, and meetings should land automatically once they exist:

`kb granola import <file.json>` turns exported meetings into markdown under the
directory named by `[granola] notes_dir` in `config.toml`. That directory is a
normal scan root, so extraction, enrichment, bundling, indexing and the graph
all handle meetings through the paths that already exist. No parallel pipeline,
no new storage, no schema change.

The export file comes from the MCP connector, which is session-scoped and
cannot run inside a scheduled job. That is why the command takes a file rather
than calling an API: the fetch is manual, the ingest is not. This supersedes
the "Scheduling" section below for Granola.

Meetings become `type: Meeting Notes`, which is already in the closed type set
in `config.toml`, so nothing about the enrichment prompt changes.

Each meeting is one concept of `type: Meeting`, with `attendees`,
`duration_minutes` and `source_app` alongside the usual frontmatter. Body order:
`# Summary` (written by the local model), `# Decisions` (one bullet each, with
the speaker), `# Action Items` (owner and any date), `# Transcript`
(speaker-labeled, timestamped).

Transcripts chunk differently: split on speaker turns, group into 400 to 600
token windows with two turns of overlap. A chunk that cuts mid-answer is useless.

Every decision and action item is promoted to its own concept, `type: Decision`
and `type: Action Item`, linked back to the parent meeting.

**Checkpoint 8B**: meeting counts per source, date range, decision and action
item counts, five full sample meeting concepts.

### 8C — entities without merging — DONE 2026-08-09

Built and running against the real corpus. 4,682 `Entity` nodes, 16,023
`MENTIONS` edges, 1,778 duplicate candidates reported in
`bundle/entity-review.md`, 0 merged. `config/entity-aliases.toml` ships with
every example commented out, so it currently merges nothing.

Extends the Phase 2 enrichment pass rather than replacing it.

- Every surface form is its own `Entity` node. `"Dana Reyes"`, `"Dana"` and
  `"dreyes@..."` are three nodes. That is correct for this phase.
- Node properties: `surface_form`, `kind`, `first_seen`, `occurrence_count`,
  `sources`.
- `MENTIONS` edges from every concept that names the entity. Fireflies speaker
  labels and attendee lists become entities too.

Duplicates are reported, never merged:

- Candidates from string distance, shared email domains, and co-occurrence in
  the same meeting.
- Written to `bundle/entity-review.md`, sorted by occurrence count.
- **Never merged automatically, and never on verbal approval in chat.** Merges
  happen only through `config/entity-aliases.toml`, edited by hand.
- The alias file applies at graph build time, not extraction time, so every
  merge stays reversible.

**Checkpoint 8C**: total entity nodes, top 30 by occurrence, top 20 duplicate
candidates.

### Retrieval and MCP changes — DONE, mostly before Phase 8 started

Built during Phase 3 and Phase 6 under "built for Phase 8 now, not migrated
later", and verified against the real corpus on 2026-08-09 rather than taken on
trust: `--type Runbook` filters, `--sensitivity personal` returns 15 hits, and
`traverse` over `MENTIONS` returns neighbours annotated with the shared entity
(`"via": "Redwood"`).

The eval rerun happened on the same day, on the full corpus, and moved nothing
at hit@5 because there are no cloud sources to drown anything.

It also produced a wrong recommendation, corrected 2026-08-10. The ablation said
the reranker contributed 0.000 to hit@1, which is true and beside the point:
every eval question had an answer in the corpus, so the harness could only ask
whether the reranker reorders a list that already contains the answer. It cannot
ask whether the system admits when there is no answer, which is most of real
use. **The reranker is ON.** Its score is the only calibrated relevance signal
in the pipeline, and it is what lets the interface say "I found nothing" instead
of presenting the nearest miss as a hit. The eval now scores abstention and
confidence, and `weak_score` in `config.toml` sets the bar. See DECISIONS.md.

- `source` on every chunk: `local`, `granola`, and `gdrive` if 8A is ever
  revisited. `onedrive` and `fireflies` are not coming: OneDrive was dropped
  2026-08-09 and Fireflies 2026-08-10, both because Mike does not use them.
- `source_filter` parameter on `search_knowledge`.
- `concept_type` filter, so meetings, decisions or documents can be searched alone.
- `traverse` follows `MENTIONS` edges from an entity back to concepts.
- Rerun the Phase 5 eval set after indexing and report whether hit rate at 5
  moved. A drop means transcript chunks are drowning documents, and the fix is
  source-aware fusion weights.

### Scheduling

`kb cloud-sync` runs 8A and 8B as one command, wired to the existing launchd job
on a daily schedule. Cloud sync never goes in the FSEvents watcher.

### Failure rules

- One remote being down never stops the other.
- One bad transcript never stops the run.
- A failed OAuth refresh writes a clear error and exits non-zero, with no silent
  retry.
- Every run appends to `bundle/log.md` with counts and errors.

### Non-goals for Phase 8

Gmail, Calendar, Linear, ClickUp, HubSpot, TickTick, QuickBooks, Stripe,
Mercury. Any change-event or activity log. Automatic entity merging. Writing
anything back to any cloud service. Keeping original cloud files on disk past
extraction.

## What Phase 8 changes about phases 3 to 7

Three things are cheap now and expensive to retrofit, so they are being built in
during Phase 3 rather than migrated later:

- Every chunk record carries `source` (defaulting to `local`), `concept_type`
  and `sensitivity`, so the Phase 8 filters need no schema migration.
- `sensitivity` accepts `unknown` as a third value alongside `work` and
  `personal`, because 8A requires it and refuses to guess.
- The chunker takes a strategy, so 8B's speaker-turn chunking slots in beside
  the heading-based one instead of replacing it.
