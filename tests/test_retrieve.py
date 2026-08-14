"""Hybrid retrieval: query sanitising, fusion, filters, the pipeline, eval."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from kb.evaluate import load_questions, run_eval
from kb.index import run_index
from kb.retrieve import (
    Hit,
    _file_path_from,
    _filter_clause,
    bm25_search,
    reciprocal_rank_fusion,
    sanitize_fts,
    search,
)

from test_index import StubEmbedder  # noqa: F401  (reused stub embedder)


# --- FTS5 query sanitising ---------------------------------------------------


@pytest.mark.parametrize(
    "query,expected",
    [
        ("restart widget", '"restart" OR "widget"'),
        ("  spaced   out  ", '"spaced" OR "out"'),
        ("a b cd", '"cd"'),                       # single characters dropped
        ("", ""),
        ("???", ""),
    ],
)
def test_sanitize_fts(query, expected):
    assert sanitize_fts(query) == expected


def test_sanitize_fts_strips_match_syntax():
    """A stray quote or operator must become data, never syntax."""
    out = sanitize_fts('restart NEAR/3 "widget" AND (foo OR bar)*')
    assert '(' not in out and ')' not in out and '*' not in out
    assert out.count('"') % 2 == 0


def test_bm25_survives_a_hostile_query(cfg, tmp_path):
    """Whatever the user types, the query must not raise."""
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.fts_path)
    conn.execute(
        "CREATE VIRTUAL TABLE chunks USING fts5(chunk_id UNINDEXED, "
        "concept_id UNINDEXED, source UNINDEXED, concept_type UNINDEXED, "
        "sensitivity UNINDEXED, title, heading_path, text)"
    )
    conn.execute(
        "INSERT INTO chunks VALUES ('c#0','a/b','local','Note','work','T','','hello world')"
    )
    conn.commit()
    conn.close()
    for hostile in ['" OR 1=1 --', "NEAR(", "*", "^^^", "a" * 500]:
        assert isinstance(bm25_search(cfg, hostile, 5, None, None, None), list)


# --- fusion ------------------------------------------------------------------


def test_rrf_rewards_agreement():
    a = ["x", "y", "z"]
    b = ["y", "x", "w"]
    scores = reciprocal_rank_fusion([a, b], k=60)
    # y is 2nd and 1st; x is 1st and 2nd. Equal, and both beat single-list items.
    assert scores["y"] == pytest.approx(scores["x"])
    assert scores["x"] > scores["z"]
    assert scores["x"] > scores["w"]


def test_rrf_k_damps_the_top_rank():
    a, b = ["x"], ["y", "x"]
    small = reciprocal_rank_fusion([a, b], k=1)
    large = reciprocal_rank_fusion([a, b], k=1000)
    # With a large k the gap between rank 1 and rank 2 nearly vanishes.
    assert (small["x"] - small["y"]) > (large["x"] - large["y"])


def test_rrf_handles_empty_lists():
    assert reciprocal_rank_fusion([[], []], k=60) == {}


def test_rrf_ranks_start_at_one():
    assert reciprocal_rank_fusion([["x"]], k=60)["x"] == pytest.approx(1 / 61)


# --- filters -----------------------------------------------------------------


def test_filter_clause_builds_only_what_is_given():
    assert _filter_clause(None, None, None) == ("", [])
    assert _filter_clause("work", None, None) == ("sensitivity = ?", ["work"])
    clause, values = _filter_clause("work", "gdrive", "Runbook")
    assert clause == "sensitivity = ? AND source = ? AND concept_type = ?"
    assert values == ["work", "gdrive", "Runbook"]


# --- hit helpers -------------------------------------------------------------


def test_snippet_truncates_and_collapses_whitespace():
    h = Hit("c#0", "a/b", "T", "", "one\n\n  two   three", "local", "Note", "work")
    assert h.snippet(200) == "one two three"
    assert h.snippet(5).endswith("...")


def test_score_prefers_the_reranker_when_present():
    h = Hit("c#0", "a/b", "T", "", "x", "local", "Note", "work", rrf=0.5)
    assert h.score == 0.5
    h.rerank = 0.9
    assert h.score == 0.9


def test_file_path_decodes_a_uri_with_spaces():
    fm = {"resource": "file:///Users/example/My%20Projects/a%20b.md"}
    assert _file_path_from(fm) == "/Users/example/My Projects/a b.md"


def test_file_path_falls_back_to_sources():
    fm = {"sources": [{"resource": "file:///tmp/x%20y.pdf"}]}
    assert _file_path_from(fm) == "/tmp/x y.pdf"


def test_file_path_empty_when_not_a_file_uri():
    assert _file_path_from({"resource": "https://example.com/x"}) == ""


# --- the pipeline ------------------------------------------------------------


@pytest.fixture
def indexed(cfg, monkeypatch):
    """A small real index: two concepts, real chunks, stub embeddings."""
    import kb.embed as embed_mod

    b = cfg.bundle_dir
    (b / "runbook").mkdir(parents=True)
    for name, h, body in (
        ("restart", "aa11", "Stop the widget service, wait thirty seconds, start it."),
        ("invoice", "bb22", "Quarterly revenue reached four million dollars in total."),
    ):
        (b / "runbook" / f"{name}.md").write_text(
            f"---\ntype: Runbook\ntitle: {name.title()}\n"
            f"description: About {name}.\nsensitivity: work\n"
            f"resource: file:///Users/example/My%20Docs/{name}.md\n"
            f"source_hash: {h}\n---\n\n# Body\n\nSee [other](/runbook/other.md).\n"
        )
        out = cfg.extract_out_dir / h[:2] / f"{h}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"# Detail\n\n{body}\n")
    (b / "index.md").write_text('---\nokf_version: "0.2"\n---\n\n# All\n')

    monkeypatch.setattr(embed_mod, "check_embed_model", lambda c: (True, "stub"))
    monkeypatch.setattr(embed_mod, "Embedder", StubEmbedder)
    monkeypatch.setattr("kb.retrieve.Embedder", StubEmbedder)
    embed_mod._TABLE_CACHE.clear()
    run_index(cfg, show_progress=False)
    return cfg


def test_search_returns_ranked_hits(indexed, monkeypatch):
    monkeypatch.setattr("kb.retrieve.rerank", lambda cfg, q, hits: None)
    hits, timings = search(indexed, "widget service restart", limit=5)
    assert hits
    assert timings["returned"] == len(hits)
    assert all(h.rrf > 0 or h.from_graph for h in hits)


def test_search_attaches_frontmatter_and_file_path(indexed, monkeypatch):
    monkeypatch.setattr("kb.retrieve.rerank", lambda cfg, q, hits: None)
    hits, _ = search(indexed, "widget", limit=5)
    top = hits[0]
    assert top.concept.get("type") == "Runbook"
    assert top.file_path.startswith("/Users/example/My Docs/")  # decoded from the URI


def test_search_honours_the_concept_type_filter(indexed, monkeypatch):
    monkeypatch.setattr("kb.retrieve.rerank", lambda cfg, q, hits: None)
    hits, _ = search(indexed, "widget", limit=5, concept_type_filter="Nonexistent")
    assert hits == []


def test_search_honours_the_sensitivity_filter(indexed, monkeypatch):
    monkeypatch.setattr("kb.retrieve.rerank", lambda cfg, q, hits: None)
    assert search(indexed, "widget", limit=5, sensitivity_filter="personal")[0] == []
    assert search(indexed, "widget", limit=5, sensitivity_filter="work")[0]


def test_search_honours_the_source_filter(indexed, monkeypatch):
    """Phase 8 adds cloud sources; the filter must already work."""
    monkeypatch.setattr("kb.retrieve.rerank", lambda cfg, q, hits: None)
    assert search(indexed, "widget", limit=5, source_filter="gdrive")[0] == []
    assert search(indexed, "widget", limit=5, source_filter="local")[0]


def test_search_records_which_retriever_found_what(indexed, monkeypatch):
    monkeypatch.setattr("kb.retrieve.rerank", lambda cfg, q, hits: None)
    hits, _ = search(indexed, "widget service", limit=10)
    assert any(h.vector_rank is not None for h in hits)
    assert any(h.bm25_rank is not None for h in hits)


def test_search_survives_a_missing_graph(indexed, monkeypatch):
    """The graph is optional. Retrieval must degrade, not fail."""
    monkeypatch.setattr("kb.retrieve.rerank", lambda cfg, q, hits: None)
    assert not indexed.graph_dir.exists()
    hits, timings = search(indexed, "widget", limit=5)
    assert hits and timings["graph_neighbours"] == 0


def test_search_uses_the_reranker_score_for_the_final_order(indexed, monkeypatch):
    def fake_rerank(cfg, query, hits):
        # Reverse the fused order, so the final order can only come from here.
        for i, h in enumerate(sorted(hits, key=lambda h: h.rrf)):
            h.rerank = 1.0 - i * 0.01

    monkeypatch.setattr("kb.retrieve.rerank", fake_rerank)
    hits, _ = search(indexed, "widget service", limit=5)
    assert hits[0].rerank >= hits[-1].rerank
    assert hits[0].rerank > 0


def test_empty_query_does_not_crash(indexed, monkeypatch):
    monkeypatch.setattr("kb.retrieve.rerank", lambda cfg, q, hits: None)
    hits, _ = search(indexed, "", limit=5)
    assert isinstance(hits, list)


# --- eval harness ------------------------------------------------------------


def test_shipped_eval_set_loads(cfg):
    from kb.config import repo_root

    real = replace(cfg, root_dir=repo_root())
    questions = load_questions(real)
    answerable = [q for q in questions if q["concepts"]]
    unanswerable = [q for q in questions if not q["concepts"]]

    # The shipped set is a template, so the count is not fixed. What must hold is
    # the shape: real questions, and both kinds present.
    assert answerable, "the set needs questions the corpus can answer"
    assert all(q["q"] for q in questions), "every question needs text"
    assert unanswerable, (
        "the set needs questions the corpus cannot answer, or the eval can only "
        "score reordering and never abstention"
    )


def test_eval_scores_a_perfect_and_a_hopeless_retriever(indexed, tmp_path, monkeypatch):
    monkeypatch.setattr("kb.retrieve.rerank", lambda cfg, q, hits: None)
    qfile = tmp_path / "q.toml"
    qfile.write_text(
        '[[question]]\nq = "widget service restart"\n'
        'concepts = ["runbook/restart"]\n\n'
        '[[question]]\nq = "widget service restart"\n'
        'concepts = ["nothing/here"]\n'
    )
    stats = run_eval(indexed, qfile, limit=5, show_progress=False)
    assert stats["questions"] == 2
    assert 0.0 < stats["hit_at_5"] < 1.0        # one hit, one miss
    assert len(stats["misses"]) == 1
    assert stats["latency_ms"]["median"] > 0


def test_eval_refuses_a_missing_file(cfg, tmp_path):
    with pytest.raises(FileNotFoundError):
        load_questions(cfg, tmp_path / "nope.toml")


def test_search_raises_on_embed_failure_by_default(indexed, monkeypatch):
    """The CLI, the MCP server and the eval harness all rely on this."""
    def boom(*a, **k):
        raise RuntimeError("ollama is not running")

    monkeypatch.setattr("kb.retrieve.Embedder", boom)
    with pytest.raises(RuntimeError):
        search(indexed, "widget service", limit=5)


def test_search_can_degrade_to_keyword_only(indexed, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ollama is not running")

    monkeypatch.setattr("kb.retrieve.Embedder", boom)
    monkeypatch.setattr("kb.retrieve.rerank", lambda cfg, q, hits: None)
    hits, timings = search(indexed, "widget service", limit=5,
                           on_embed_error="degrade")
    assert hits, "BM25 should still find the widget runbook"
    assert timings["degraded"] == "semantic search unavailable"


def test_a_healthy_search_reports_no_degradation(indexed, monkeypatch):
    monkeypatch.setattr("kb.retrieve.rerank", lambda cfg, q, hits: None)
    _, timings = search(indexed, "widget service", limit=5)
    assert timings["degraded"] == ""


# --- Abstention: questions the corpus cannot answer ---------------------------


def _eval_file(tmp_path, body):
    p = tmp_path / "questions.toml"
    p.write_text(body)
    return p


def test_a_question_with_no_expected_concepts_is_scored_for_abstention(indexed, tmp_path, monkeypatch):
    """The gap that produced bad advice about the reranker.

    Every question in the original set has an answer in the corpus, so the eval
    could only ever ask "does the reranker reorder a list that already contains
    the answer". It cannot ask the question that matters in real use: when
    there is no answer, does the system say so.
    """
    monkeypatch.setattr("kb.retrieve.rerank", lambda cfg, q, hits: None)
    path = _eval_file(tmp_path, """
[[question]]
q = "Stop the widget service, wait thirty seconds"
concepts = ["runbook/restart"]

[[question]]
q = "the airspeed velocity of an unladen swallow"
concepts = []
""")
    report = run_eval(indexed, path=path, show_progress=False)

    assert report["answerable"] == 1
    assert report["unanswerable"] == 1
    # hit rates describe answerable questions only, or an unanswerable one
    # counts as a permanent miss and drags the number down for being correct.
    assert report["hit_at_1"] == 1.000


def test_abstention_is_correct_when_a_hopeless_query_scores_low(indexed, tmp_path, monkeypatch):
    monkeypatch.setattr("kb.retrieve.rerank", lambda cfg, q, hits: None)
    path = _eval_file(tmp_path, """
[[question]]
q = "zzzz qqqq vvvv nothing like this exists"
concepts = []
""")
    report = run_eval(indexed, path=path, show_progress=False)
    assert report["abstention"] == 1.000, "no answer exists, so weak is the right call"


def test_an_all_answerable_set_reports_no_abstention_score(indexed, tmp_path, monkeypatch):
    """Nothing to abstain on means the number would be meaningless."""
    monkeypatch.setattr("kb.retrieve.rerank", lambda cfg, q, hits: None)
    path = _eval_file(tmp_path, """
[[question]]
q = "Stop the widget service"
concepts = ["runbook/restart"]
""")
    report = run_eval(indexed, path=path, show_progress=False)
    assert report["unanswerable"] == 0
    assert report["abstention"] is None


def test_abstaining_on_everything_scores_zero_confidence(indexed, tmp_path, monkeypatch):
    """Abstention alone is trivially perfect for a system that never commits.

    Without the reranker every fused score sits near 0.016, so the system
    abstains on answerable questions too. Measured on the real corpus
    2026-08-10: abstention 1.000 with the reranker on AND off, which looks like
    a tie and is a stuck needle. Confidence is what separates them.
    """
    monkeypatch.setattr("kb.retrieve.rerank", lambda cfg, q, hits: None)
    path = _eval_file(tmp_path, """
[[question]]
q = "Stop the widget service, wait thirty seconds"
concepts = ["runbook/restart"]

[[question]]
q = "zzzz qqqq vvvv nothing like this"
concepts = []
""")
    report = run_eval(indexed, path=path, show_progress=False)

    assert report["abstention"] == 1.000, "correctly says nothing matched"
    assert report["confidence"] == 0.000, (
        "but it also refuses to stand behind the answer it did find, which is "
        "what makes the abstention score meaningless on its own"
    )
    assert report["false_doubt"], "the answerable question it doubted is listed"
