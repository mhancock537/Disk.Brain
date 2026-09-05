# AI Agent Guide for Disk.Brain

This guide helps AI agents (Claude, GPT, etc.) work effectively with the Disk.Brain codebase.

## Quick Start for Agents

If you're an AI agent assigned to work on Disk.Brain:

1. **Read these files first** (in order):
   - `README.md` - Project overview and setup
   - `.cursorrules` - Complete agent configuration (this is your primary reference)
   - `DECISIONS.md` - Rationale for all design choices
   - `ROADMAP.md` - What's built, what's not, and why

2. **Understand the architecture:**
   - Seven phases: manifest → extract → enrich → bundle → index → graph → incremental
   - Local-only, read-only, privacy-first
   - Everything is resumable and rebuildable

3. **Before making changes:**
   - Verify your assumptions (check versions, test APIs, measure if needed)
   - Read `DECISIONS.md` for relevant context
   - Use test fixtures (`tests/conftest.py`), never real documents

4. **When modifying code:**
   - Match surrounding style
   - Update `DECISIONS.md` with any new rationale
   - Write tests for both valid and invalid cases
   - Run `pytest tests/` before committing

## Common Tasks for AI Agents

### Adding a New File Format

1. Add extraction logic to `src/kb/extract/`
2. Register the extension in the appropriate `EXTS` constant
3. Add test fixtures to `tests/conftest.py`
4. Test extraction: `pytest tests/extract/`

**Example commit:** See DECISIONS.md Phase 1 for `textutil` addition

### Modifying Retrieval Logic

1. Changes go in `src/kb/retrieve.py`
2. Update eval: `kb eval --ablation`
3. Check metrics: hit@1, hit@5, MRR, abstention, confidence
4. Update benchmarks in `DECISIONS.md` if behavior changes significantly

**Testing:** Run eval before and after, compare ablation tables

### Adding a New Concept Type

1. Edit `config.toml`: Add to `[enrich] concept_types` with definition
2. No code changes needed (types are data-driven)
3. Test: `kb enrich --limit 5` on relevant documents
4. Verify: `kb validate` should still pass

### Modifying the MCP Interface

1. Tool implementations in `mcp_server.py` (module-level `_impl_*` functions)
2. Schemas derived from type hints and docstrings
3. Test stdout hygiene: `tests/test_mcp_server.py::test_stdout_stays_clean`
4. Test with inspector: `npx @modelcontextprotocol/inspector --cli`

**Critical:** Never print to stdout (corrupts JSON-RPC protocol)

### Optimizing Performance

**Before optimizing:**
- Profile first (don't guess)
- Check if it's a one-time cost (e.g., model loading) vs. per-query cost
- Consider that embedding/enrichment are expected to be slow (GPU-bound)

**Safe to optimize:**
- Chunking logic (pure Python, CPU-bound)
- Graph queries (Cypher optimization)
- FTS5 queries (query construction, not SQLite itself)

**Measure, don't assume:**
- Use `kb eval --ablation` for retrieval changes
- Use `kb embed-bench` for embedding changes
- Use `time` for command-line operations

### Fixing a Bug

**Process:**
1. Write a failing test that reproduces the bug
2. Fix the bug
3. Verify the test passes
4. Add an entry to `DECISIONS.md` if the bug revealed a non-obvious issue
5. Run full test suite: `pytest tests/`

**Example:** See DECISIONS.md Phase 2 "Entity nodes: casefolded key" for entity key collision bug

## Understanding Test Fixtures

The test corpus is defined in `tests/conftest.py`:

```python
@pytest.fixture
def corpus_dir(tmp_path):
    # Creates 20-file corpus with:
    # - Various formats (PDF, .docx, .md, .txt)
    # - Text of different lengths
    # - Some with headings, some without
    # - Mix of types (docs, notes, code)
    ...
```

**Using fixtures in tests:**
```python
def test_something(corpus_dir):
    # corpus_dir is a Path to a temporary directory
    # containing the 20-file test corpus
    manifest = Manifest(db_path=corpus_dir / "test.db")
    manifest.scan(roots=[corpus_dir])
    ...
```

**Never:**
- Hard-code real file paths
- Use real document content
- Reference real person/company names

**Always:**
- Use the fixture-generated corpus
- Create synthetic test data
- Use placeholder names (e.g., "Acme Corp", "Widget Project")

## OKF Format Deep Dive

### Concept File Structure

```markdown
---
okf_version: "0.2"
type: Technical Documentation
title: Example Concept
generated:
  by: okf-kb/qwen3:14b-q4_K_M
  at: "2026-08-07T12:34:56Z"
timestamp: "2026-08-07T12:34:56Z"
source_hash: abc123...
status: active
tags:
  - kubernetes
  - networking
entities:
  - kind: technology
    name: Kubernetes
  - kind: organization
    name: Acme Corp
related:
  - /technical-documentation/kubernetes-setup
---

# Example Concept

One-sentence summary goes here.

## Introduction

Body content...
```

### Validation Rules

**Errors (fail build):**
- Missing frontmatter
- Missing or empty `type`
- Frontmatter in `index.md` (should only have okf_version)
- `log.md` heading not `## YYYY-MM-DD`

**Warnings (logged, don't fail):**
- Broken cross-links (tolerated per OKF §6.1)
- Missing `index.md`
- Missing recommended fields
- Out-of-order log headings

**Run validation:** `kb validate`

### Cross-linking

Links are scored by shared entities/tags:
- Entity match: 1.5x weight
- Tag match: 1.0x weight
- Rare entities/tags score higher (1/log2(df+1))
- Only top `max_related` links above `min_link_score` kept
- All thresholds in `config.toml`

**Implementation:** See `bundle.py::_compute_cross_links()`

## Database Schema

### Manifest Database (`data/manifest.db`)

**Tables:**
- `files`: Source files (path, size, mtime, inode, hash, mime, scan_status)
- `concepts`: Enriched concepts (concept_id, source_hash, title, type, enrich_status)

**Key columns:**
- `scan_status`: `included`, `excluded`, `no_route`, `missing`
- `extract_status`: `pending`, `ok`, `failed`
- `enrich_status`: `pending`, `ok`, `failed`

### Vector Index (`data/vector.lance`)

**Schema:**
- `concept_id`: str
- `chunk_id`: str  
- `vector`: list[float]  (1,024 dims for qwen3-embedding:0.6b)
- `source`: str  (local, granola, gdrive)
- `concept_type`: str
- `sensitivity`: str  (work, personal, unknown)
- `text`: str

### BM25 Index (`data/bm25.db`)

**Table: `chunks`**
- `concept_id`: str
- `chunk_id`: str
- `source`: str
- `concept_type`: str
- `sensitivity`: str
- `text`: str (indexed by FTS5)

### Graph (`data/graph.db`)

**Node tables:**
- `Concept`: concept_id, title, type, sensitivity, source, file_path
- `File`: file_path, hash
- `Entity`: name (kind:casefolded), kind, surface_form, first_seen, occurrence_count
- `Tag`: name (casefolded)

**Relationship tables:**
- `LINKS_TO`: Concept → Concept (cross-references)
- `CHILD_OF`: Concept → Concept (hierarchy, currently empty)
- `MENTIONS`: Concept → Entity → Concept (indirect)
- `TAGGED_AS`: Concept → Tag → Concept (indirect)

## Configuration Schema

Key sections from `config.toml`:

```toml
# Scan roots
[[scan.roots]]
path = "/Users/you/Documents"
enabled = true
sensitivity = "personal"

# Denylist (gitignore-style patterns)
[scan]
deny = [
    ".git", "*.git",
    "node_modules",
    "*/Disk.Brain/*",  # Don't index ourselves
]

# Enrichment
[enrich]
model = "qwen3:14b-q4_K_M"
base_url = "http://127.0.0.1:11434"

# Embedding  
[embed]
model = "qwen3-embedding:0.6b"
base_url = "http://127.0.0.1:11434"

# Retrieval
[retrieve]
weak_score = 0.10  # Abstention threshold

[retrieve.rerank]
enabled = true  # Essential for abstention
model = "mlx-community/Qwen3-Reranker-0.6B-4bit"
```

## Debugging Guide

### Enable Verbose Logging

```bash
# Set in config.toml
log_level = "DEBUG"

# Or per-command
kb search "query" --verbose
```

### Check Pipeline Status

```bash
kb report              # Show extraction status table
kb validate            # Check OKF conformance
kb cypher "MATCH (n) RETURN labels(n), count(*)"  # Node counts
```

### Test MCP Integration

```bash
# Generate config
kb mcp-config

# Test with inspector
npx @modelcontextprotocol/inspector --cli

# Test programmatically
python -c "
from kb.mcp_server import _impl_search
from kb.config import load_config
cfg = load_config()
print(_impl_search(cfg, 'test query'))
"
```

### Profile Slow Operations

```python
import cProfile
import pstats

# Profile a function
profiler = cProfile.Profile()
profiler.enable()

# ... code to profile ...

profiler.disable()
stats = pstats.Stats(profiler)
stats.sort_stats('cumulative')
stats.print_stats(20)
```

## Anti-Patterns to Avoid

❌ **Don't:**
- Assume library APIs from training data (verify first)
- Add dependencies without checking if they install/work
- Make operations non-resumable (add checkpointing)
- Send data off-machine (breaks privacy guarantee)
- Commit `bundle/` or `data/` (contains user data)
- Use real file paths in tests
- Guess at performance (measure first)
- Patch indexes (rebuild instead for derived data)
- Invent new config locations (use `config.toml`)

✅ **Do:**
- Verify versions/APIs before writing code
- Check DECISIONS.md for prior art
- Make long operations resumable
- Keep everything local
- Use test fixtures exclusively  
- Profile before optimizing
- Rebuild derived data (bundle, indexes, graph)
- Centralize config in `config.toml`
- Update documentation with code changes

## Performance Characteristics to Keep in Mind

**GPU-bound operations** (expected to be slow):
- Enrichment: ~17 sec/doc
- Embedding: ~11 chunks/sec
- Reranking: ~2.4 sec/query

**CPU-bound operations** (should be fast):
- Scanning: ~2 sec for 3K files
- Chunking: milliseconds
- Graph queries: milliseconds

**I/O-bound operations**:
- Extraction: varies by format (OCR is slow)
- Bundle writing: seconds

**Don't optimize GPU-bound operations** (you can't beat the hardware). **Do optimize** CPU and I/O-bound operations if they're slow.

## Code Review Checklist

When reviewing changes (or reviewing your own code):

- [ ] Tests added for new functionality
- [ ] Tests cover both valid and invalid inputs
- [ ] `pytest tests/` passes
- [ ] No real file paths or document content in code
- [ ] `bundle/` and `data/` remain gitignored
- [ ] Privacy constraints respected (no data off-machine)
- [ ] Long operations are resumable
- [ ] `DECISIONS.md` updated if design choice made
- [ ] Documentation updated for user-facing changes
- [ ] No unintentional logging to stdout in MCP code

## Resources

**Primary documentation:**
- `.cursorrules` - Complete agent configuration
- `DECISIONS.md` - Design rationale for everything non-obvious
- `ROADMAP.md` - What's built, what's planned, what's dropped
- `CONTRIBUTING.md` - Contribution guidelines

**Specifications:**
- OKF v0.2: github.com/GoogleCloudPlatform/knowledge-catalog at commit `3fcbb9f`
- MCP: modelcontextprotocol.org/docs

**Dependencies:**
- Ollama: ollama.com/download
- MLX: github.com/ml-explore/mlx
- LanceDB: lancedb.github.io/lancedb/
- LadybugDB: github.com/LadybugDB/ladybug

**Testing:**
- pytest: docs.pytest.org
- Fixtures: See `tests/conftest.py`

---

## Example: Adding Support for RTF Files

Let's walk through a complete example of extending Disk.Brain.

### Step 1: Verify the capability exists

```bash
# Check if textutil can handle RTF
echo "test" | textutil -convert txt -stdin -stdout
```

Result: It works (textutil is already used, see `extract/office.py`)

### Step 2: Find where to add it

```bash
grep -r "RTF" src/
grep -r ".rtf" src/
```

Result: Already supported! See `extract/office.py` line 47.

### Step 3: If it wasn't supported...

Would add to `extract/office.py`:

```python
# Add to TEXTUTIL_EXTS
TEXTUTIL_EXTS = {".rtf", ".doc", ".pages", ".numbers", ".key"}  # .rtf already here
```

### Step 4: Add a test

```python
# In tests/extract/test_office.py
def test_extract_rtf(tmp_path):
    rtf_file = tmp_path / "test.rtf"
    rtf_file.write_bytes(b"{\\rtf1\\ansi Hello World}")
    
    result = extract_office(rtf_file)
    
    assert result.status == "ok"
    assert "Hello World" in result.text
```

### Step 5: Test and document

```bash
pytest tests/extract/test_office.py::test_extract_rtf
```

If this was a new addition, would add to DECISIONS.md:

```markdown
### RTF support via textutil

macOS `textutil` handles RTF natively. Added `.rtf` to `TEXTUTIL_EXTS` in
`extract/office.py`. No new dependencies needed; textutil ships with macOS.
```

---

This guide should give you everything you need to work effectively with Disk.Brain. When in doubt, check `.cursorrules` for the complete reference.
