# Decisions

Every substitution, deviation and judgement call, with the reason. Newest phase last.

## Phase 1 — manifest and extraction (2026-08-07)

### Verified before writing code

Nothing here was taken from training data. Each was checked this session.

| Thing | Verified value | How |
| --- | --- | --- |
| uv | 0.10.0 | `uv --version` |
| Python | 3.12.12 (uv-managed) | `uv python list` |
| blake3 | 1.0.9 | PyPI JSON API |
| PyMuPDF | 1.28.2 | PyPI, then `pymupdf.__version__` in the venv |
| MarkItDown | 0.1.7 | PyPI; `MarkItDown.convert()` returns `.text_content` (introspected) |
| ocrmac | 1.0.1 | PyPI; `OCR(image, framework, recognition_level, ...)`, `.recognize()` → `[(text, conf, bbox)]` (introspected) |
| pandoc | 3.8.3 | `pandoc --version` |
| Ollama | 0.13.5, server started, **0 models pulled** | `ollama --version`, `ollama list` |
| LadybugDB | PyPI name is **`ladybug`**, not `ladybugdb`; v0.19.1, released 2026-08-04 | PyPI 404 on `ladybugdb`; README at github.com/LadybugDB/ladybug states `pip install ladybug` |

### The graph database installs, no substitution needed

`ladybugdb` does not exist on PyPI. The package is published as **`ladybug`**. It is
the Kuzu fork, confirmed by the README line "formerly known as Kuzu". Version 0.19.1
ships a `cp312-macosx_15_0_arm64` wheel (4 MB), so it installs on this machine without
a build. Neo4j and the SQLite-CTE fallback are not needed. Install is deferred to
Phase 4; only the wheel's existence was verified here.

### Repo root is the checkout, not a nested `okf-kb/`

The spec's tree drew `okf-kb/` as the outer directory. The checkout is already a
project directory, so nesting `okf-kb/` inside would add a level for nothing. The package is still `src/kb/` and the distribution is still named
`okf-kb`. `bundle/` sits at the repo root, which keeps concept IDs and the
root-relative links OKF requires unambiguous.

### MIME detection uses the standard library, no new dependency

The manifest needs a `mime` column but the agreed stack names no MIME tool, so any
library here would have been an addition requiring sign-off. `mimetypes.guess_type`
handles the extension cases; a 12-entry magic-byte table in `manifest.py` covers
extensionless files and refines the OOXML/EPUB `PK\x03\x04` collision. `python-magic`
was rejected because it needs Homebrew `libmagic`. Google's `magika` is present as a
transitive dependency of MarkItDown, but depending on another package's transitive
dependency is fragile, so it is unused.

### Two dependencies added beyond the agreed stack — needs sign-off

The working rule says ask before adding any dependency not listed. Two are in
`pyproject.toml` that are not in the stack table:

- **`typer` 0.27.1** — the CLI framework behind `kb scan` / `kb extract`. The
  alternative is stdlib `argparse`, which would work; typer is less code and gives
  the `--help` output for free.
- **`rich` 15.0.0** — the progress bar with a time estimate that Phase 3 explicitly
  requires, and the report tables.

Both are pure Python with no system dependencies. Flagging rather than assuming:
say the word and either can come out.

`pytest` is also present, taken as implied by "write tests as you go".

### The project excludes itself from its own corpus

`~/Projects` is an indexed root and this repo lives inside it. Denying only `data/` would have left `bundle/` exposed,
so from Phase 2 onward every scan would ingest its own generated concepts as source
documents and the corpus would grow on each run, with Phase 7's watcher turning that
into a live loop. The denylist now excludes the whole project directory.

### `textutil` added as a third extraction engine

macOS ships `/usr/bin/textutil`, which is the only thing on this machine that reads
legacy `.doc`, and it also handles `.rtf`, `.pages`, `.numbers` and `.key`. It is a
system binary, not a package, so it adds nothing to install. Chain order per
extension lives in `extract/office.py`: MarkItDown for OOXML and HTML, pandoc for
EPUB/ODF, textutil for the Apple and legacy formats. Falling through the chain is
normal; only an all-engines failure is recorded as a failure.

### Scanned-PDF threshold, stated as numbers

The brief said "character count per page" without a number. Chosen, all in
`config.toml` so they can be tuned without touching code:

- a page is thin below **100 characters** of extractable text
- **8 pages** are sampled, evenly spaced through the document
- the document routes to full OCR when **60%** of sampled pages are thin
- OCR is capped at **40 pages** per document, rendered at **200 DPI**

The test is per page, not per document, so a mostly-digital PDF with a few scanned
inserts gets a hybrid pass: text pages kept as text, thin pages OCR'd and appended.

### `no_route` scan status

A file whose extension has no extractor is recorded but never hashed. That kept
blake3 off 5,061 files and 509.6 MB, mostly PNG screenshots, on the real corpus. The
manifest stays a complete census of what was seen; it just does not pay to hash what
nothing can read.

### `include_source_code`, defaulting to **false**

The first real scan returned 10,453 files against an expected corpus of 2,000 to
5,000. The excess was source code and machine config. Splitting the text extensions
into prose (`DOC_TEXT_EXTS`) and code (`CODE_TEXT_EXTS`) and defaulting code off
gives 3,054 files, inside the expected range. Measured both ways on 2026-08-07:

| setting | included | size |
| --- | --- | --- |
| `include_source_code = false` | 3,054 files | 164.6 MB |
| `include_source_code = true` | 6,461 files | 283.4 MB |

**This is Mike's call, not mine.** One line in `config.toml` and a rerun of
`kb scan` switches it.

### Denylist additions found by running it

Added after the first real scan surfaced them: `*.git` (bare repos like
`project-mirror.git`, which the plain `.git` pattern misses), `*.pack`/`*.idx` (git
object packs), `worktrees`/`.worktrees` (duplicate checkouts of trees already
indexed), `blobs` (OCI image layers, which appear as extensionless gzip), and
`graphify-out` (generated).

### Binary detection in the plain-text reader

The encoding ladder ends at latin-1, which decodes every possible byte, so it can
never fail and would silently turn a binary into mojibake. `looks_binary()` runs
first: any NUL byte, or more than 5% control characters in the first 8 KB, and the
file is refused. Thresholds match what `file(1)` and git use. A UTF-16 BOM is
exempted, since UTF-16 text is legitimately full of NULs. Caught by
`test_plain_rejects_binary`.

### Read-only, and staying that way

No code path opens a source file for writing. Cloud-evicted (dataless) files are
detected via `st_flags & SF_DATALESS` and recorded rather than read, because reading
one forces a download, which is a write side effect against a read-only constraint.
On this machine the check found none: `~/Documents` and `~/Desktop` are not
iCloud-synced, and there are zero `.icloud` stubs.

### Not needed, and why

- **Full Disk Access**: never requested. All nine roots were probed and every one is
  readable, including a real byte-level read from `~/Documents`.
- **`~/Library`**: excluded by the hard constraint. Note that
  `~/Library/Mobile Documents/com~apple~CloudDocs` holds 109 entries and roughly
  4.5 GB of iCloud Drive content that is therefore out of scope for this build.

### Open, for Phase 5

`qwen3-reranker` is **not** in the official Ollama library. It is available as the
community model `dengcao/Qwen3-Reranker-0.6B` on Ollama and as
`mlx-community/Qwen3-Reranker-0.6B-mxfp8` on Hugging Face. Both satisfy "Ollama or
MLX", so this is a sourcing question rather than a substitution, but it needs a
decision before Phase 5 and it depends on whether Ollama exposes a first-class
scoring endpoint at all.

---

## Phase 2 — OKF bundle generation (2026-08-07)

### Spec version, pinned

Written against **OKF v0.2**, `okf/SPEC.md` at commit
`3fcbb9f828c2f23d109c855ee403c3a4c81f3a96`. Checked against upstream the same
day: `git log 3fcbb9f..origin/main -- okf/` returns nothing, so the local clone
is identical to `origin/main` at `930b65fc3f5619d5d0591f88c72ebae8b848d60d`. The
spec was read from `git show origin/main:okf/SPEC.md` rather than by pulling, so
the clone's working tree was never touched.

### The brief says `timestamp`; v0.2 renamed it. Both are written

Spec §13.1 is a deliberate breaking change: `timestamp` is superseded by
`generated: { by, at }`. The brief asks for `timestamp` on every concept.

Both are written, with identical values. §4.1 explicitly permits extra keys and
§13.1 tells consumers they MAY fall back to a legacy `timestamp`, so nothing is
lost either way. `generated.by` uses the spec's actor convention (§7):
`okf-kb/qwen3:14b-q4_K_M`.

The same reasoning settles Phase 7's deleted files. The brief says mark the
concept deprecated in frontmatter; §5.4 already defines `status: deprecated`
for exactly that, so Phase 7 will use the spec-native field rather than invent
a key.

### Verified against the running model before the loop was written

| Question | Answer | How |
| --- | --- | --- |
| Correct Ollama tag | `qwen3:14b-q4_K_M`. **`qwen3:14b-q4` does not exist** and fails to pull | registry manifest probe; the tags page listing is not a valid ref |
| Can thinking be turned off | Yes, `think=False` on `ollama.chat` | client signature introspected, then 8 real calls with no `<think>` block in any reply |
| Does constrained JSON work | Yes, `format=<json schema>` | 8/8 replies parsed, all `concept_type` values inside the enum |
| Does parallelism help | **No** | 3 concurrent requests: throughput flat (23.3 s/doc vs 22.3), latency tripled. The GPU is already saturated by one request |

### Measured enrichment throughput

Three samples, because the first two were not representative. `enrichable()`
originally ordered by `word_count DESC`, so `--limit N` took the largest
documents and both the timing and the sample concepts were skewed. It now
orders by hash, which is stable across runs and uncorrelated with size.

| Sample | Model | Per document | 2,560 documents |
| --- | --- | --- | --- |
| 10 hash-ordered (representative) | `qwen3:14b-q4_K_M` | **16.7 s** | **about 12 hours** |
| 12 largest | `qwen3:14b-q4_K_M` | 31.2 s | 22 hours |
| 6 mid-size | `qwen3:14b-q4_K_M` | 22.3 s | 16 hours |
| 6 mid-size | `qwen3:8b` | 13.4 s | 9.5 hours |

The head-to-head is the mid-size pair: the 8B is 1.7x faster. Applied to the
representative rate that is roughly 7 hours, extrapolated rather than measured.

Quality on the same 8 documents was close. The 14B produced better titles
(`Widget Launch Campaign Brief` against the 8B's lowercase
`widget launch campaign brief`). Each made one type error the other did not.
**Model choice is Mike's**, and it is one line in `config.toml`.

### A closed set of concept types

The brief gave six type examples. Left open, the model returns `Meeting Notes`,
`Meeting note` and `MeetingNotes` as three types, which is three directories,
and the concept ID is the file path. The set is closed to fifteen named types in
`config.toml`, each with a one-line definition that is fed to the model, and the
JSON schema enum makes an out-of-set answer impossible rather than merely
unlikely. Adding a type is one line in the config.

The definitions matter. The first benchmark, with bare type names and no
descriptions, filed a campaign brief as `Contract` and an integration FAQ as
`Financial Record`.

### Concept IDs are allocated once and frozen

The concept ID is the file path (§2), and the tree is organised by inferred
type, so the model controls the ID. Phase 7 re-enriches on a content change. If
a rerun infers a different type, the path would move, the ID would change, and
every inbound `/type/slug.md` link would break.

So `concepts.concept_id` is written once, keyed by `source_hash`, and the upsert
uses `COALESCE(concepts.concept_id, excluded.concept_id)`. A later type change
updates the frontmatter and leaves the file where it is. Covered by
`test_concept_id_is_frozen_across_reruns`.

Collisions are handled deterministically: the corpus holds many `README.md`,
`CLAUDE.md` and `index.html`, and titles collide too. A colliding slug gains a
prefix of the source hash, so the same document lands on the same ID every run.
`index` and `log` are rejected as concept filenames, since §3.1 reserves them.

### Validator: errors versus warnings

§11 lists three conformance conditions and then spends a paragraph telling
consumers what they MUST NOT reject a bundle over. The validator splits along
that line.

**Errors** (fail the run): a non-reserved `.md` with no parseable frontmatter or
no non-empty `type`; frontmatter in a non-root `index.md`; a root `index.md`
carrying anything but `okf_version`; a `log.md` heading that is not
`## YYYY-MM-DD`.

**Warnings** (logged, never fail): broken cross-links (§6.1 requires tolerating
them), missing `index.md`, missing recommended keys, log headings out of order.

The subtle one: §11.1 says "every **non-reserved** `.md` file", and §8 says index
files carry no frontmatter. A validator that demanded frontmatter everywhere
would reject the index files it just generated.

Every rejection has a matching acceptance test, plus a test that the correction
the error names actually passes, plus a mutation check that neuters the type
guard and confirms the suite goes red.

### Cross-links are scored, not enumerated

"Propose cross-links between concepts that share entities or tags" over 2,560
concepts is 3.3 million pairs, and a tag like `finance` matches hundreds of
documents.

An inverted index reaches only concepts that actually share a term. A term's
weight falls as it gets more common (`1/log2(df+1)`), a term appearing in more
than `link_rarity_ceiling` concepts contributes nothing at all, and an entity
match counts 1.5x a tag match. Each concept keeps its top `max_related` peers
above `min_link_score`. All four numbers are in `config.toml`.

Entity keys are `kind:casefolded-name`, so `Acme Corp` and `ACME CORP` match
while `Falcon` the project and `Falcon` the system do not.

### Two modules beyond the brief's layout

`okf.py` (format primitives, atomic writes, validator) and `bundle.py` (concept
rendering, cross-links, index and log generation). The brief's `enrich.py` holds
the LLM work only. Splitting the spec implementation from the LLM call keeps the
validator testable without a model running.

### One more dependency, and one already approved

- **`pyyaml` 6.0.3** — OKF frontmatter is YAML, so a YAML library is unavoidable.
  Flagging it for the same reason as `typer` and `rich`.
- **`ollama` 0.6.2** — the official Python client for the agreed enrichment
  runtime. Covered by the stack table, listed here for completeness.

### Writes are atomic, resume trusts the database

Concepts are written to a temp file and `os.replace`d into position, so a crash
leaves either the old file or the new one, never a truncated one. Resume state
lives in the `concepts` table keyed by source hash, never inferred from which
files exist: a half-written file on disk looks complete, and the database does
not. Enrichment commits after every document, so an interrupt costs at most one.

### Enrichment excludes sources that stopped being sources

`enrichable()` filters on `scan_status = 'included'` as well as
`extract_status = 'ok'`. A file that later falls under a new deny glob becomes
`missing` while keeping its old successful extraction, and must not go on
producing a concept.

---

## Phase 3 — chunk, embed, index (2026-08-07)

### Verified before writing code

| Thing | Result |
| --- | --- |
| SQLite FTS5 | available, 3.50.4, with `porter unicode61` stemming. Confirmed `MATCH 'jumping'` hits "jumps" |
| LanceDB | 0.36.0, table create, add and vector search all work |
| `ollama.embed` | takes a list, returns `EmbedResponse.embeddings`. `qwen3-embedding:0.6b` is 1,024 dimensions, `:4b` is 2,560 |
| LadybugDB (Phase 4) | 0.19.1 installs on Apple silicon, node and rel tables created, a Cypher traversal returned the expected row |

### Token counting without a tokenizer

The brief specifies an 800-token budget, but nothing in the agreed stack counts
tokens, and adding `transformers` would mean a second copy of the vocabulary on
disk. Instead the ratio was **measured** against the enrichment model itself:
`ollama.generate(..., num_predict=1)` reports `prompt_eval_count`, and 12 real
corpus documents totalling 48,000 characters gave a median of **3.67 characters
per token** (range 3.24 to 4.37). That constant lives in `chunk.py` with the
measurement recorded beside it.

### A real bug the first full pass caught

The first corpus-wide chunking run produced 35,420 chunks, of which **2,867
exceeded the 800-token budget**, the worst at 1,601 tokens, which is exactly
twice the limit.

Cause: when a chunk was flushed, the overlap tail was taken as whole pieces, and
the guard that stopped the tail overrunning its budget only applied if the tail
already held something. A single trailing piece at the full size limit was
therefore carried over whole, and the next piece pushed the chunk to 2x.

Two fixes: the tail is truncated to the overlap budget at a word boundary, and
the carried overlap is dropped outright when it would push the incoming piece
over the limit. After the fix: **34,995 chunks, maximum exactly 800 tokens, zero
over budget**. Locked in by `test_no_chunk_ever_exceeds_the_budget`, which
asserts the bound at three different budgets.

### Chunks come from the extracted text, not from the concept files

The brief says `kb index` rebuilds from `bundle/`. Read literally, that means
chunking the concept files, which are metadata records of roughly 200 words
each. That would give about 2,500 chunks. The brief's own estimate is 40,000 to
60,000, which is only reachable from full document text.

So the bundle **drives** the work and the extraction **supplies** the prose:
every concept's frontmatter carries `source_hash`, which locates its body under
`data/extracted/`. The result is 34,995 chunks, inside the expected band. Both
halves stay rebuildable, the bundle from the manifest and the extraction from
the source files, so nothing about the artifact-of-record arrangement changes.

Each concept's one-sentence description is also emitted as chunk 0. It is the
best short summary of the document that exists, and without it a query matching
the summary but no body paragraph would return nothing.

### Every chunk carries its heading path into the embedding

A chunk reading "It renews annually" is meaningless alone. `embed_input()`
prepends the concept title and the heading path, so the model sees
"Master Services Agreement > Term and Renewal" above the text. The raw text is what
gets stored and returned; the context only shapes the vector.

### Embedding benchmark: held-out sentence probes

There are no relevance labels for this corpus, and generating them with an LLM
would measure the LLM. So the probe is built from the corpus: lift a distinctive
sentence out of a chunk, index that chunk **with the sentence removed**, then
query with the sentence. The removal is the whole point. The query shares no
exact span with its target, so a hit means the model placed the sentence near
the passage it came from rather than matching a substring.

500 chunks, 150 probes, both models:

| Model | Dims | recall@1 | recall@5 | recall@10 | MRR | chunks/s |
| --- | --- | --- | --- | --- | --- | --- |
| `qwen3-embedding:0.6b` | 1,024 | 0.587 | 0.773 | 0.840 | 0.670 | 11.7 |
| `qwen3-embedding:4b` | 2,560 | 0.640 | **0.847** | 0.887 | 0.733 | 1.9 |

The 4B is 7.4 points better at recall@5 and **6.2x slower**. Full-corpus
projections: 0.6B in 50 to 68 minutes, 4B in 5.2 to 7.0 hours. The range comes
from measured rates of 8.6 chunks/s on large chunks and 11.7 on benchmark
chunks; the corpus mean of 263 tokens per chunk sits below both.

**Recommendation: 0.6B for the first full pass.** Re-embedding needs no
re-enrichment, so upgrading later is a standalone 5 to 7 hour job rather than a
repeat of everything. Getting the system working end to end costs an hour; the
7 points can be bought afterwards if retrieval actually feels short.

### No ANN index

At 35,000 vectors a flat scan is exact and takes milliseconds. IVF or HNSW would
trade recall away for a speedup nothing needs. `use_ann = false` in
`config.toml` records the choice rather than leaving it implicit.

Vectors are L2-normalised on write, so cosine similarity is a plain dot product
and stored magnitudes cannot skew a ranking.

### Both indexes are dropped and rebuilt, never patched

`kb index` recreates the LanceDB table and the FTS5 table from scratch. A
changed embedding model changes the vector width, and a stale table of the wrong
dimension is worse than no table at all. Phase 7 will add incremental updates
for single concepts; the full rebuild stays the safe path.

### Built for Phase 8 now, not migrated later

Three things were cheap today and expensive to retrofit, so they are already in:

- every chunk carries `source` (defaulting to `local`), `concept_type` and
  `sensitivity`, which are exactly the Phase 8 filter columns
- `sensitivity` accepts `unknown` as a third value, because 8A requires it and
  refuses to guess
- the chunker dispatches through a `STRATEGIES` table, so 8B's speaker-turn
  chunking is a new function beside the markdown one rather than a rewrite

Deprecated concepts (§5.4) are read but excluded from both indexes. They stay in
the bundle for links and history, and out of retrieval because they are not
current.

---

## Phase 4 — graph (2026-08-07)

### LadybugDB, no substitution

`pip install ladybug` 0.19.1 on a `cp312-macosx_15_0_arm64` wheel. Node tables,
rel tables and a Cypher traversal all verified on this machine before any
loader code was written. Neo4j and the SQLite-CTE fallback are not needed.

One surprise worth recording: **LadybugDB writes a single file, not a
directory**, plus `.wal` and `.tmp` siblings. A `shutil.rmtree` on rebuild
therefore raised `NotADirectoryError`. `_remove_graph()` now handles both
shapes and sweeps the sidecars, so a future version that switches back to a
directory cannot strand a stale store that the next build silently reuses.
Connections and databases are closed explicitly, since an open handle blocks
the delete.

### Bulk load through Parquet, not row-by-row CREATE

`COPY ... FROM` a Parquet file: 5,000 nodes in 0.04s, 4,000 relationships in
0.02s, measured before committing to the approach. The real 22-concept build
takes 0.63s end to end. At full corpus scale this stays a seconds-long
operation, which is what makes "drop and rebuild" the right model: the graph is
derived, never authoritative, so a schema change is a rebuild and never a
migration.

Duplicate edges are collapsed before loading. The same tag listed twice on one
concept would otherwise double a count that people will read as a fact.

### CHILD_OF is implemented and currently empty

The brief names five edge types. Four of them populate. `CHILD_OF` is zero,
and that is the correct answer, not a bug.

OKF §6.1 calls parent/child "the implicit hierarchy", which in OKF is the
directory tree. Our bundle is `<type>/<slug>.md`, exactly one level deep, so no
concept has a concept above it. The code walks up the path and creates the edge
whenever a parent concept exists, proven by
`test_child_of_follows_bundle_nesting`, which adds a nested concept and gets the
edge. It will populate the moment the tree nests.

The alternative would be inventing a `Directory` or `Type` node so every concept
has a parent, which means adding a sixth node type the brief did not ask for.
Flagged rather than done.

### Entity nodes: casefolded key, kind-scoped

The node key is `kind:casefolded-name`, so `Acme Corp` and `ACME CORP` are one
node while `Falcon` the project and `Falcon` the system stay two. Distinct
surface forms stay distinct on purpose: the first real build produced `Ada`,
`Ada Lovelace` and `Adelaide Lovelace` as three separate nodes, which is exactly
what Phase 8C specifies.

Properties carry the 8C shape early where it is additive: `first_seen` (earliest
`generated.at` across mentioning concepts) and `occurrence_count`. The brief's
Phase 4 wording says the property is `name`; 8C calls it `surface_form`. `name`
is used, because that is what the Cypher queries read, and a rename is a
seconds-long rebuild.

### Three worked Cypher queries ship with the code

`kb cypher --list` prints them, `kb cypher <name>` runs them, and `kb cypher
"<statement>"` runs anything. They live in `EXAMPLE_QUERIES` in `graph.py` and
each one is exercised by a parametrised test, so a schema change that breaks a
query fails the suite rather than the demo.

---

## Phase 5 — hybrid retrieval (2026-08-07)

### The reranker runs on MLX. Ollama could not do it

Flagged at the Phase 1 checkpoint as needing a decision before Phase 5. It
resolved itself empirically:

| Attempt | Result |
| --- | --- |
| `ollama pull qwen3-reranker` | no such model in the official library |
| `ollama pull dengcao/Qwen3-Reranker-0.6B` | 404. That community model has no `latest` tag, only `Q8_0` and `F16` |
| `dengcao/Qwen3-Reranker-0.6B:Q8_0` | pulls, then emits `!!!!` for every prompt. A broken GGUF conversion |
| Ollama `logprobs: true` | returns `null`, even for a known-good model |

Ollama exposes no rerank or scoring endpoint, so the yes/no logit trick needs
logprobs, and those do not work. The stack allows "Ollama or MLX", and MLX
works: `mlx-community/Qwen3-Reranker-0.6B-4bit` loads in 13.7s and scores a
query-document pair from the softmax over the `yes` and `no` logits at the final
position. Sanity check on four pairs: 0.9988 for a direct answer, 0.2451 for a
tangential mention, 0.0001 for an unrelated sentence.

This is a runtime change, not a model substitution. The model is the
Qwen3-Reranker 0.6B the brief named.

### Latency: 8,960 ms down to a 2,381 ms median

The first working query took 8.96s, of which the reranker was 7.18s. Three
fixes, each measured:

| Change | Effect |
| --- | --- |
| Batch the reranker's forward passes (32 at a time, sorted by length) | 7.2s to 4.3s |
| Cap candidates at 30 instead of 60 | 4.3s to 3.3s |
| Cache the LanceDB table handle instead of reopening per query | vector stage 738ms to 526ms |

Median across the 20-question eval is **2,381 ms**, p90 3,528 ms. Warm-process
numbers, which is the case that matters: the MCP server in Phase 6 is a
long-lived process, so the reranker load and the table handle are paid once.

Right padding is safe for the batched forward pass because the model is causal:
pad tokens sit after the real ones and cannot influence any earlier position.
Each row's logits are read at its own true final index.

### The eval scores 20/20, and that number is not yet meaningful

Every configuration scores hit@1 = 1.000 on the 20-question set. So does
fusion alone, with no reranker and no graph hop, at a 67 ms median.

The eval set is **saturated** because the corpus is 22 concepts. Twenty
questions over twenty-two documents is nearly a lookup. The harness is correct,
the questions are real, and the number is true, but it cannot currently
distinguish a good pipeline from an adequate one.

The ablation table exists precisely so this stays visible:

| Configuration | hit@1 | MRR | median ms |
| --- | --- | --- | --- |
| full pipeline | 1.000 | 1.000 | 2,452 |
| no reranker | 1.000 | 1.000 | 90 |
| no graph hop | 1.000 | 1.000 | 2,478 |
| fusion only | 1.000 | 1.000 | 67 |

**Rerun `kb eval --ablation` after the full corpus lands.** If the reranker
still adds nothing at 2,425 concepts, turning it off is a 27x latency win for
free, and that is a real decision waiting on real data. The eval set should also
grow: twenty questions over twenty-two documents is thin, and the same twenty
over 2,425 will be a different test.

### FTS5 queries are sanitised, not escaped

A user query is data. FTS5 treats `"`, `*`, `NEAR`, and parentheses as syntax,
so punctuation is stripped, terms shorter than two characters dropped, and what
remains quoted and OR'd. OR rather than AND, so a query with one absent word
still scores instead of returning nothing. Five hostile inputs, including
`" OR 1=1 --`, are covered by a test that only asserts the call returns a list.

### The graph hop is optional by construction

`graph_neighbours` returns an empty set when the graph is missing or the query
fails, and logs it. Retrieval degrades to vector plus BM25 rather than failing,
because the graph is derived data and its absence is a rebuild away from being
fixed. Covered by `test_search_survives_a_missing_graph`.

### Phase 8 filters are live now

`search()` already takes `sensitivity_filter`, `source_filter` and
`concept_type_filter`, applied as a LanceDB prefilter and a SQL predicate on the
BM25 side. Filter values come from closed enum-like sets, never free text.
Tests assert that `source_filter="gdrive"` returns nothing today and that
`"local"` returns results, so the Phase 8 wiring is proven before the data
exists.

---

## Phase 6 — MCP server (2026-08-07)

### The MCP SDK moved. `FastMCP` is gone in 2.0

`mcp` 2.0.0 has no `mcp.server.fastmcp`. The class is `MCPServer` in
`mcp.server.mcpserver`, with `@server.tool(...)` and `server.run("stdio")`.
Found by walking the package rather than by assuming the 1.x API. Tool schemas
come from the type annotations and the Google-style docstring `Args:` block, so
the inspector shows a full `inputSchema` for all three tools with no manual
JSON.

### stdout hygiene is asserted, not assumed

The brief requires stdout to stay clean. Two things threatened it: `httpx` logs
an INFO line per Ollama call, and `huggingface_hub` prints a tqdm progress bar
when it checks the reranker repo. Both were quietened
(`NOISY_LOGGERS`), and the invariant is now covered by a test that spawns the
real server as a subprocess, runs `initialize` and `tools/list`, and asserts
**every** stdout line parses as JSON.

Measured on a live session including a real search and a real traverse: 3 stdout
lines, 0 non-JSON, 12 stderr lines. The stderr count is the point. The logs
exist, they are just not in the protocol stream.

### The concept ID is a path, so it is treated as hostile input

`get_concept` turns `concept_id` into a filesystem path. It is resolved and then
checked with `Path.relative_to(bundle)`, which rejects anything landing outside
the bundle. Four traversal attempts are covered by tests, including
`../../../../etc/passwd` and an absolute `/etc/passwd`. Single quotes in a
concept ID are doubled before they reach Cypher.

### A tool answers, it never crashes

The first version only caught `FileNotFoundError`, which meant a stopped Ollama
or an unpulled embedding model propagated an exception out of the tool. In an
MCP client that is an unexplained failure mid-conversation.

Now every backend failure returns a structured error with an actionable hint
("check that Ollama is running... `kb doctor`"). Found by a test, not in
production.

### `traverse` handles indirect edges

A concept does not link to another concept via MENTIONS; it *shares an Entity*
with it. So MENTIONS and TAGGED_AS traverse through the intermediate node
(`Concept -> Entity <- Concept`) while LINKS_TO and CHILD_OF are direct. The
result names the shared entity in `via`, so a neighbour arrives with its reason
attached: "reached via Acme Security" rather than an unexplained edge.

One neighbour reached by two edge types is returned once with two reasons. Live
example from the proving bundle: `plan/q3-launch-timeline` is a neighbour of
the market analysis report both by an explicit cross-link and by
sharing the entity `Acme Security`.

This also satisfies the Phase 8 requirement to follow MENTIONS from an entity
back to concepts, which is why it was built this way now.

### Verified with the official inspector

`npx @modelcontextprotocol/inspector --cli`. Two argument forms failed first:
the bare `-m kb.mcp_server` target had its `-m` swallowed as an inspector
option, and `--tool-arg` shell quoting mangled the values. What works is a
config file plus `--tool-args-json`.

---

## Phase 7 — incremental updates (2026-08-07)

### Your three decisions, as built

**Cheap work inline, expensive work queued.** The watcher does stat, hash,
extract and mark-deleted, all of which finish in milliseconds. Enrichment and
embedding are queued and run by `kb drain`, capped at `[drain] max_documents`.
Measured live: a new file went from creation to extracted in **4 seconds**, and
the drain that enriched, rewrote and reindexed it took **14.2 seconds** total.

**Graph rebuilt, not patched.** A full rebuild is a bulk Parquet load and cannot
drift out of sync; a patch is more code and more failure modes. Config flag
`[drain] rebuild_graph`.

**Watching `$HOME`.** One FSEvents stream over the whole home directory
rather than nine.

### Watching is wider than indexing, and that is deliberate

Watching `~/` and indexing `~/` are not the same decision. Measured before
building: **12,166 routable documents live outside the nine configured roots**.
Indexing them would take the corpus from 2,425 to about 15,000 and enrichment
from 11 hours to roughly 70.

So a path enters the pipeline only if it passes the denylist **and** sits under
an enabled `[[scan.roots]]` entry (`[watch] require_configured_root`). The
watcher sees everything; the corpus grows only when you edit the roots list.
Turn the flag off and the whole home directory becomes the corpus.

### The queue is the manifest, not a new structure

`files.extract_status = 'pending'` is already the extraction queue and
`concepts.enrich_status != 'ok'` is already the enrichment queue. A changed file
clears its hash, which resets both, and the work reappears without a scheduler.
Nothing new to keep consistent.

The drain enriches **recent first** (`enrichable(recent_first=True)`), so a file
you just changed does not wait behind a 2,400-document backlog. Bulk passes keep
hash order, which is stable and uncorrelated with size.

### Moves are free

A rename keeps the blake3 hash. Concept IDs are keyed on that hash and frozen,
so a moved file is recognised (`known_hash`), never re-extracted, and never
re-enriched. That fell out of the Phase 2 design rather than needing new code.

### Deletion uses the spec's own field

Spec §5.4 defines `status: deprecated` for exactly this, so no key was invented.
The concept file stays on disk for links and history, `read_bundle` already
excludes deprecated concepts, and `update_index` drops their chunks.
`undeprecate()` reverses it when a file comes back from the Trash.

Demonstrated end to end: created a note, watcher extracted it in 4s, drain
enriched it into `source-notes/watcher-demo-note`, deleted the file, and the
drain marked it `status: deprecated`, `deprecated_at: 2026-08-07T21:46:56Z`.
**Concept file still 1,377 bytes on disk. FTS chunks: 0.**

### Incremental indexing was the real work

`kb index` drops and rebuilds both stores, which is a 50-minute embedding pass
at full corpus size and therefore not a per-file operation. `update_index()`
deletes and reinserts by `concept_id` in both LanceDB and FTS5. Verified that
LanceDB supports `table.delete("concept_id IN (...)")` before building on it.

### A bug caught the hard way

`cap = limit or cfg.drain.max_documents` treated `--limit 0` as absent and fell
through to the configured cap of 200. Running `kb drain --limit 0` to demo the
deprecation path with no enrichment therefore started a 200-document run. It
was killed after 44 documents, roughly 12 minutes of GPU that had been
explicitly declined.

Fixed to `cfg.drain.max_documents if limit is None else max(0, int(limit))`, and
covered by `test_drain_limit_zero_enriches_nothing`. The 44 documents are real
work that reduces tonight's queue from 2,403 to 2,359, but the run should not
have happened.

### Backpressure

The change queue caps at `[watch] max_queue` (5,000). Above that it drops events
and logs loudly, telling you to run `kb scan` to catch up. Dropping with a loud
warning beats unbounded memory growth, and a full scan is 2 seconds.

### Agents ship uninstalled

`scripts/install-agents.sh` installs both. It is deliberately not run yet: the
22:00 pipeline holds the manifest for 11 hours, and a second writer during that
is asking for trouble. Install after the pipeline reports complete.

- **watch**: `KeepAlive`, runs continuously, never touches the GPU.
- **drain**: 02:00 daily, capped. `kb drain -n 5` gives an immediate result
  after changing one file.

---

## Duty-cycled enrichment (2026-08-08)

### The overnight run lost 8.8 hours to sleep

Started 22:14, and by 09:32 had enriched only 146 of 2,425 documents. The
per-document gaps told the story: seven stalls totalling about 8.8 hours,
separated by clean runs at 15 to 30 seconds each. `pmset -g log` confirmed
"Entering Sleep state due to 'Maintenance Sleep' ... Using Batt", waking on lid
open at 09:09.

`caffeinate -dimsu` blocks idle sleep but **not lid-close sleep on battery**.
That combination was flagged in the script comment and at the checkpoint, and it
is exactly what happened. Nothing was lost or corrupted: every stage resumes,
and the 146 documents were real.

### The battery drain is severe, and the guard now reflects it

Measured on 2026-08-08: **61% to 4% in about 33 minutes** under sustained
enrichment load, with `pmset` reporting "0:42 remaining" at 61%. The M4 Pro GPU
at full tilt outruns the battery badly.

So the charge floor applies on **AC as well as battery**. At a low charge the
adapter barely keeps ahead of the draw, so "plugged in" is not by itself a safe
state to work from. Below 30%, the cycle rests another window and re-checks.

### 2 hours off, 2 hours on

Mike's call, to let the battery recover. `scripts/run-duty-cycle.sh` starts with
an **off** window, then alternates, and stops when the queue empties before
running bundle, index and graph.

Supporting change: `kb enrich --max-seconds` gives a wall-clock budget checked
**between** documents, so a window always ends on a clean boundary with its work
committed. Never mid-document.

At the healthy rate of about 21 seconds per document, 2,194 remaining documents
need roughly 12.8 hours of on-time, so about 26 hours of wall clock at a 50%
duty cycle, assuming the machine stays awake and charged during on-windows.

### One owner, enforced

A PID lock file. A second copy would double-enrich and fight for the GPU. Learnt
immediately: relaunching after an edit left two instances alive because the kill
had not landed before the lock file was removed. Both were reconciled to a
single owner, and the lock is now the authority.

The 22:00 `com.diskbrain.pipeline` LaunchAgent was booted out, so it cannot
start a competing run tonight.

---

## The corpus landed, and Phase 8 shrank (2026-08-09)

### Enrichment finished, and the numbers finally mean something

2,425 documents enriched, bundle CONFORMANT against OKF v0.2 with 0 errors,
0 warnings and 19,433 internal links. 34,995 chunks indexed. Graph: 2,426
Concept, 2,426 File, 4,682 Entity, 3,212 Tag nodes.

The run took two days and cost two restarts. Friday night lost 8.8 hours to
sleep. Saturday lost the afternoon to a hard power-off at roughly 13:00, which
also proved that Ollama did not auto-start: every ON window after the reboot
failed instantly with "Ollama not reachable" and the duty cycle rested two
hours between each failure. `brew services start ollama` closes that for good.

### The reranker is off

The Phase 5 eval was saturated at 22 concepts and said nothing. Against 2,425
it finally discriminates, and it says the reranker earns nothing:

| configuration | hit@1 | hit@5 | MRR | median ms |
| --- | --- | --- | --- | --- |
| full pipeline | 0.850 | 1.000 | 0.908 | 2,750 |
| no reranker | 0.850 | 1.000 | 0.910 | 473 |
| fusion only | 0.850 | 1.000 | 0.910 | 102 |

Zero change to hit@1, MRR fractionally *better* without it, and 2,648 ms of a
2,750 ms query. Live queries went 4,584 ms to 989 ms. `[retrieve.rerank]
enabled = false`, the code stays, the flag reverses it.

Honest limit: hit@5 is saturated at 1.000, so 20 questions cannot discriminate
above rank 5. This is "no help on these 20", not "no help ever". hit@3 did slip
from 1.000 to 0.950, one question falling out of the top three.

### 8A died on its own verification step

The section said "verify rclone version and remote reachability before writing
pipeline code". Doing that killed the phase. `gdrive` configured at
`scope = drive.readonly`, reachable, conf already 600. Then the survey: 330
files, **244 of them JPEGs**, and after the filters 8A already specified, the
entire phase is **79 documents and 8.1 MB**. Three percent of the corpus for a
manifest table, a hand-built diff, a staging flow and scheduler wiring.

Two spec assumptions were also wrong. There are **zero Google-native files**, so
the Docs/Sheets/Slides export machinery had nothing to act on, and the
no-hash-on-native-files trap it would have created does not exist. OneDrive is
out by Mike's call.

Skipped, on the number rather than on taste.

### Granola's cache is encrypted, so 8B changed shape

ROADMAP's preferred path was the local cache. `granola.db` is not a SQLite file
and `cache-v6.json` has an encrypted `.enc` twin, last written 2026-06-15.
Breaking into an app's private store was not attempted. Granola therefore goes
through the MCP connector, which is session-scoped and **cannot run in a
scheduled job**, so it is a manual pull and never part of `kb cloud-sync`. That
supersedes the Scheduling section for Granola.

Fireflies is blocked on a key that does not exist anywhere on this machine.

### 8C was three additions, not a subsystem

Reading the code first showed the entity graph was already built to the 8C
design: casefolded kind-scoped keys, `first_seen`, `occurrence_count`, and a
comment at graph.py:42 anticipating the `surface_form` rename. What was missing
was `surface_form` and `sources` on the node, the duplicate report, and the
alias layer.

Same for the retrieval section: `source_filter`, `concept_type` and `traverse`
over `MENTIONS` were all built in Phases 3 and 6 under "built for Phase 8 now".
Verified on the real corpus rather than trusted: the filters work and traverse
returns neighbours annotated with the shared entity.

### Reported, never merged

1,778 duplicate candidates in `bundle/entity-review.md`, rebuilt on every
`kb graph` so it cannot go stale against the graph it describes. Two signals,
both from the spec: one surface form contained in another, and an email local
part matching a name (`beta.widget@example.app` against `Widget`).
Co-occurrence does not create a pair, it annotates one, and 87 pairs have a
non-zero count.

Pairs are only ever proposed within one `kind`. `Acme` the organization and
`Acme` the project stay separate, which is the same rule that makes the merge
file safe.

`config/entity-aliases.toml` ships with every example commented out. The top
candidate is `Ada` / `Ada Lovelace` at 701 uses, and it is deliberately not
merged: `Ada` may also collide with `Ada Chen`, which is precisely the
judgement the file exists to keep in human hands. Aliases apply at graph build
time, so deleting a block and rebuilding reverses the merge. Covered by
`test_aliases_are_reversible_by_removing_the_file`.

### Two bugs the tests did not catch until the real path ran

`_pair_reason` crashed with `IndexError` on the first real-corpus rebuild: a
single-token name leaves no other token to build a first initial from, and the
unit tests only used two-token names. Reproduced as a test, then fixed.

The second was quieter and worse. The canonical spelling of a merged entity was
taken from whichever concept happened to be read first, not from the alias
file. The test suite passed on iteration-order luck. Caught by writing a case
where the canonical form appears in no document at all
(`test_the_canonical_spelling_comes_from_the_alias_file`), which failed, then
fixed by returning the spelling from `load_aliases` alongside the mapping.

### The nightly path was exercised before it ran unattended (2026-08-09)

Three things had been claimed working without being proven end to end. All
three were closed the same evening, before the 02:00 drain's first run.

**The watcher.** A file written to `~/Documents` at 18:22:41 was picked up at
18:22:47, six seconds later against a 3.0s debounce, and
`watch.err.log` recorded `extracted` at 18:22:50. 28 words, `plain` engine.

**The MCP transport.** Previous checks called the `_impl_*` functions directly,
which proves the implementation and nothing about the protocol. A probe spawned
the exact command Claude Code is registered with and spoke real JSON-RPC over
stdio: `initialize` returned `okf-kb 0.1.0`, `tools/list` returned all three
tools, and a `tools/call` on `search_knowledge` came back with real hits. One
stderr line, so stdout stayed clean JSON-RPC as Phase 6 asserts.

**The drain, in both directions.** `kb drain -n 1` enriched the queued watcher
file, rewrote the bundle, reindexed one concept into both stores (2 chunks) and
rebuilt the graph, in 23.2s. The file was then trashed and `kb drain -n 0`
marked the concept `status: deprecated`, `deprecated_at: 2026-08-09T23:24:19Z`,
dropped its 2 chunks from LanceDB and FTS5, and left the concept file on disk,
exactly as Phase 7 specifies. 13.5s.

Both runs included the `kb validate` step fixed earlier the same evening, and
the bundle validates CONFORMANT at 2,427 concepts with 0 errors. That is the
step that would have failed at 02:00.

The verification concept stays in the bundle as deprecated, matching the
`source-notes/watcher-demo-note` precedent from Phase 7. It is inert: excluded
from `read_bundle`, zero chunks in either index.

---

## The reranker goes back on, and the eval learns to see why (2026-08-10)

### I gave bad advice, and the reason is worth more than the correction

On 2026-08-09 the first full-corpus ablation said the reranker contributed
nothing: hit@1 of 0.850 with it and 0.850 without, MRR fractionally better
without, and 2,648 ms of a 2,750 ms query. I recommended turning it off. Mike
did, then reversed it on 2026-08-10. He was right.

The numbers were accurate. The measurement was the wrong one.

Every question in `eval/questions.toml` had an answer in the corpus. So the
only question the harness could ask was "does the reranker reorder a list that
already contains the answer". It does not. What it never asked is "when there
is no answer, does the system say so", which is most of real use for a tool you
reach for when you half-remember something.

### What the reranker is actually for

Measured on the live corpus, same two queries:

| query | best score | |
| --- | --- | --- |
| readiness gates blocking a pre-prod install | **0.9890** | answer exists |
| battery draining and the machine sleeping overnight | **0.0006** | no answer exists |

Three orders of magnitude. Fusion scores are reciprocal ranks: they say where
something placed, never whether it is any good, and they sit near 0.016 for
every query ever run. So with the reranker off, a search with no answer returns
the corpus's nearest neighbours looking exactly like a real hit. That is how a
Baofeng UV-5R radio manual came back as the top answer about battery drain.

### The eval now measures abstention, and its opposite

`concepts = []` marks a question the corpus cannot answer, and five are now in
the set. Two are about this project itself, which is denylisted in config.toml
and therefore permanently outside the corpus by design.

Hit rates count answerable questions only. An unanswerable one is a permanent
miss by construction, and counting it would penalise the system for being
right.

Then the trap, caught by checking the guard in both directions:

| config | hit@1 | abstention | confidence | median |
| --- | --- | --- | --- | --- |
| reranker on | 0.850 | 1.000 | **1.000** | 2,964 ms |
| reranker off | 0.850 | 1.000 | **0.000** | 599 ms |

Abstention alone reads as a tie. It is not judgement, it is a stuck needle:
without the reranker every score sits near 0.016, so the system abstains on
everything, including the twenty questions it answered correctly.
`confidence`, the rate at which it stands behind an answer it did find,
separates them completely. `false_doubt` lists the answers it refused to back.

The lesson generalises past this project: **a metric that only measures the
positive case cannot distinguish a working guard from one that always fires.**
The same shape appeared four times in the web interface build, where deleting a
guard left the whole suite green.

### The cost, stated plainly

Search goes from roughly 600 ms to roughly 3 s. That is real and it was
measured, not estimated. It buys an interface that can tell you it found
nothing, which for this tool is worth more than two seconds.

### Also shipped, both previously left out

`related` concepts in the detail pane. The approved spec asked for them and the
plan deferred them with a note, because they live in the concept body as
markdown links rather than in frontmatter. That is exactly why `/api/concept`
exists and why a search result cannot carry them. Broken links are dropped
rather than offered: OKF tolerates them (§6.1) and the bundle has some, but a
dead link is not a destination. Verified live, 7 real siblings off the
readiness gates runbook.

A non-numeric `Content-Length` crashed the handler thread instead of answering.
Flagged during Task 7 and deliberately left open at the time. Now a clean 400.
