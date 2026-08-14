"""Index build: bundle reading, FTS5, vector store, and the full rebuild."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from kb.bench import _distinctive_sentence, build_probes, evaluate
from kb.embed import Embedder, l2_normalise, open_table
from kb.index import build_chunks, build_fts, read_bundle, run_index

CONCEPT = """\
---
type: Runbook
title: Restart The Widget
description: How to restart the widget service safely.
sensitivity: work
source_hash: {h}
---

# Steps

Stop the service, wait, start it again.
"""


@pytest.fixture
def small_bundle(cfg, tmp_path):
    """A two-concept bundle with matching extracted text on disk."""
    b = cfg.bundle_dir
    (b / "runbook").mkdir(parents=True)
    for name, h in (("restart", "aa11"), ("rotate", "bb22")):
        (b / "runbook" / f"{name}.md").write_text(CONCEPT.format(h=h))
        out = cfg.extract_out_dir / h[:2] / f"{h}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            f"# Overview\n\nThe {name} procedure exists for operators.\n\n"
            f"# Detail\n\n" + ("word " * 900) + "\n"
        )
    (b / "runbook" / "index.md").write_text("# Runbook\n\n* [R](restart.md)\n")
    (b / "index.md").write_text('---\nokf_version: "0.2"\n---\n\n# All\n')
    (b / "log.md").write_text("# Log\n\n## 2026-08-07\n* **Creation**: made.\n")
    return b


# --- reading the bundle ------------------------------------------------------


def test_read_bundle_skips_reserved_files(cfg, small_bundle):
    concepts, problems = read_bundle(cfg)
    assert {c.concept_id for c in concepts} == {"runbook/restart", "runbook/rotate"}
    assert problems == []


def test_read_bundle_pairs_concepts_with_extracted_text(cfg, small_bundle):
    concepts, _ = read_bundle(cfg)
    assert all(c.text_path is not None and c.text_path.is_file() for c in concepts)


def test_read_bundle_reports_missing_text_without_failing(cfg, small_bundle):
    (cfg.extract_out_dir / "aa" / "aa11.md").unlink()
    concepts, problems = read_bundle(cfg)
    assert len(concepts) == 2
    assert any("no extracted text" in p for p in problems)


def test_read_bundle_excludes_deprecated_concepts(cfg, small_bundle):
    p = small_bundle / "runbook" / "rotate.md"
    p.write_text(p.read_text().replace("sensitivity: work", "status: deprecated\nsensitivity: work"))
    concepts, _ = read_bundle(cfg)
    assert {c.concept_id for c in concepts} == {"runbook/restart"}


def test_read_bundle_defaults_source_to_local(cfg, small_bundle):
    assert all(c.source == "local" for c in read_bundle(cfg)[0])


def test_read_bundle_on_missing_directory(cfg):
    concepts, problems = read_bundle(cfg)
    assert concepts == [] and problems


# --- chunk assembly ----------------------------------------------------------


def test_build_chunks_covers_summary_and_body(cfg, small_bundle):
    concepts, _ = read_bundle(cfg)
    chunks = build_chunks(cfg, concepts)
    assert len(chunks) > len(concepts)
    summaries = [c for c in chunks if c.heading_path == "Summary"]
    assert len(summaries) == 2
    assert all(c.concept_type == "Runbook" for c in chunks)
    assert all(c.sensitivity == "work" for c in chunks)


def test_build_chunks_tolerates_a_concept_with_no_text(cfg, small_bundle):
    (cfg.extract_out_dir / "aa" / "aa11.md").unlink()
    concepts, _ = read_bundle(cfg)
    chunks = build_chunks(cfg, concepts)
    # The summary still yields a chunk, so the concept stays searchable.
    assert any(c.concept_id == "runbook/restart" for c in chunks)


# --- FTS5 --------------------------------------------------------------------


def test_fts_build_and_bm25_query(cfg, small_bundle):
    concepts, _ = read_bundle(cfg)
    chunks = build_chunks(cfg, concepts)
    rows = build_fts(cfg, chunks)
    assert rows == len(chunks)

    conn = sqlite3.connect(cfg.fts_path)
    hits = conn.execute(
        "SELECT concept_id, bm25(chunks) FROM chunks WHERE chunks MATCH 'operator' "
        "ORDER BY bm25(chunks) LIMIT 5"
    ).fetchall()
    conn.close()
    assert hits  # porter stemming matches "operators"


def test_fts_rebuild_is_not_cumulative(cfg, small_bundle):
    concepts, _ = read_bundle(cfg)
    chunks = build_chunks(cfg, concepts)
    first = build_fts(cfg, chunks)
    second = build_fts(cfg, chunks)
    assert first == second


def test_fts_stores_the_filter_columns(cfg, small_bundle):
    concepts, _ = read_bundle(cfg)
    build_fts(cfg, build_chunks(cfg, concepts))
    conn = sqlite3.connect(cfg.fts_path)
    row = conn.execute(
        "SELECT source, concept_type, sensitivity FROM chunks LIMIT 1"
    ).fetchone()
    conn.close()
    assert row == ("local", "Runbook", "work")


# --- embedding helpers -------------------------------------------------------


def test_l2_normalise_gives_unit_length():
    v = l2_normalise([3.0, 4.0])
    assert abs(sum(x * x for x in v) - 1.0) < 1e-6


def test_l2_normalise_leaves_a_zero_vector_alone():
    assert l2_normalise([0.0, 0.0]) == [0.0, 0.0]


class StubEmbedder(Embedder):
    """Deterministic pseudo-embeddings: no model, but real vector arithmetic."""

    def encode(self, texts):
        out = []
        for t in texts:
            v = [0.0] * 16
            for i, ch in enumerate(t.lower()):
                if ch.isalpha():
                    v[ord(ch) % 16] += 1.0
            out.append(l2_normalise(v))
        return out


def test_vector_table_is_recreated_not_appended(cfg, small_bundle, monkeypatch):
    import kb.embed as embed_mod

    monkeypatch.setattr(embed_mod, "check_embed_model", lambda c: (True, "stub"))
    monkeypatch.setattr(embed_mod, "Embedder", StubEmbedder)

    concepts, _ = read_bundle(cfg)
    chunks = build_chunks(cfg, concepts)
    first, _ = embed_mod.embed_chunks(cfg, chunks, show_progress=False)
    second, _ = embed_mod.embed_chunks(cfg, chunks, show_progress=False)
    assert first == second == len(chunks)
    assert open_table(cfg).count_rows() == len(chunks)


def test_open_table_without_an_index_is_a_clear_error(cfg):
    with pytest.raises(FileNotFoundError, match="kb index"):
        open_table(cfg)


# --- full rebuild ------------------------------------------------------------


def test_run_index_end_to_end(cfg, small_bundle, monkeypatch):
    import kb.embed as embed_mod

    monkeypatch.setattr(embed_mod, "check_embed_model", lambda c: (True, "stub"))
    monkeypatch.setattr(embed_mod, "Embedder", StubEmbedder)

    stats = run_index(cfg, show_progress=False)
    assert stats["concepts"] == 2
    assert stats["chunks"] == stats["vectors"] == stats["fts_rows"]
    assert stats["tokens_max"] <= cfg.chunk.max_tokens
    assert stats["by_source"] == {"local": stats["chunks"]}
    assert stats["embed"]["dimensions"] == 16
    assert open_table(cfg).count_rows() == stats["chunks"]
    assert cfg.fts_path.is_file()


def test_run_index_refuses_an_empty_bundle(cfg):
    with pytest.raises(RuntimeError, match="no concepts"):
        run_index(cfg, show_progress=False)


# --- benchmark internals -----------------------------------------------------


def test_distinctive_sentence_skips_table_rows():
    text = (
        "| a | b | c | d | e | f | g | h | i | j |\n"
        "This is a proper sentence with enough words to qualify as a probe."
    )
    assert _distinctive_sentence(text).startswith("This is a proper")


def test_distinctive_sentence_returns_none_when_nothing_qualifies():
    assert _distinctive_sentence("Too short. Also short.") is None


def test_build_probes_removes_the_query_from_its_own_chunk():
    """The probe sentence must not survive in the chunk it points at.

    Without the removal the benchmark would be measuring exact substring
    overlap, which any embedding model gets right.
    """
    chunks = [
        "Some leading context that is long enough to matter here. "
        "The widget must be restarted before the nightly batch job runs. "
        "A third sentence adds more filler so the stripped chunk stays fair. "
        "A fourth sentence keeps the remaining word count comfortably above "
        "the floor that build_probes enforces on every candidate chunk.",
        "An unrelated chunk about invoices and accounts payable reconciliation "
        "with plenty of additional words to make it a fair distractor here.",
    ]
    indexed, probes = build_probes(chunks, count=1)
    assert len(probes) == 1
    query, target = probes[0]
    assert len(query.split()) >= 9
    assert query in chunks[target]        # it came from that chunk
    assert query not in indexed[target]   # and it is gone from the indexed copy
    assert len(indexed[target].split()) >= 25


def test_build_probes_leaves_other_chunks_untouched():
    chunks = [
        "Some leading context that is long enough to matter here. "
        "The widget must be restarted before the nightly batch job runs. "
        "A third sentence adds more filler so the stripped chunk stays fair. "
        "A fourth sentence keeps the remaining word count comfortably above "
        "the floor that build_probes enforces on every candidate chunk.",
        "An unrelated chunk about invoices and accounts payable reconciliation "
        "with plenty of additional words to make it a fair distractor here.",
    ]
    indexed, probes = build_probes(chunks, count=1)
    untouched = [i for i in range(len(chunks)) if i != probes[0][1]]
    for i in untouched:
        assert indexed[i] == chunks[i]


def test_evaluate_scores_a_perfect_retriever(monkeypatch):
    """A retriever that always ranks the target first must score 1.0."""
    docs = ["alpha beta gamma", "delta epsilon zeta", "eta theta iota"]

    class Perfect(StubEmbedder):
        pass

    monkeypatch.setattr("kb.bench.Embedder", Perfect)
    probes = [(docs[i], i) for i in range(len(docs))]
    result = evaluate("stub", docs, probes, batch_size=8)
    assert result.recall_at_1 == 1.0
    assert result.mrr == 1.0
    assert result.dimensions == 16


def test_reserved_build_artefacts_are_not_read_as_concepts(cfg):
    """Third place with the same hardcoded pair.

    read_bundle skipped {index.md, log.md} literally, so entity-review.md, which
    `kb graph` writes into the bundle, was parsed as a concept, failed, and
    logged "unparseable concept" on every single index run. Harmless noise that
    is indistinguishable from a real corruption warning, which is what makes it
    worth removing.
    """
    b = cfg.bundle_dir
    (b / "runbook").mkdir(parents=True, exist_ok=True)
    (b / "index.md").write_text("# All\n")
    (b / "log.md").write_text("# Log\n")
    (b / "entity-review.md").write_text("# Entity review\n\nNo frontmatter here.\n")
    (b / "runbook" / "real.md").write_text(
        "---\ntype: Runbook\ntitle: Real\ndescription: d\nsource_hash: aa11\n---\n\n# B\n"
    )

    _, problems = read_bundle(cfg)

    assert not any("unparseable" in p for p in problems), problems
    assert not any("entity-review" in p for p in problems), problems


def test_a_genuinely_unparseable_concept_is_still_reported(cfg):
    """The warning has to keep working, or removing the noise removes the signal."""
    b = cfg.bundle_dir / "runbook"
    b.mkdir(parents=True, exist_ok=True)
    (b / "broken.md").write_text("no frontmatter at all\n")
    _, problems = read_bundle(cfg)
    assert any("broken.md" in p for p in problems), problems
