"""MCP server: tool behaviour, argument guards, and stdout hygiene.

The stdout test is the one that matters most. A single stray print anywhere in
the import graph corrupts the JSON-RPC stream and the server dies silently in a
client, so it is asserted against a real subprocess rather than mocked.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kb.mcp_server import (
    EDGE_TYPES,
    _clean_id,
    _escape,
    _impl_get_concept,
    _impl_search,
    _impl_traverse,
    build_server,
)

REPO = Path(__file__).resolve().parent.parent


# --- helpers -----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("runbook/a", "runbook/a"),
        ("  runbook/a.md  ", "runbook/a"),
        ("/runbook/a/", "runbook/a"),
    ],
)
def test_clean_id(raw, expected):
    assert _clean_id(raw) == expected


def test_escape_doubles_single_quotes():
    assert _escape("O'Brien") == "O''Brien"


def test_edge_types_are_the_documented_set():
    assert EDGE_TYPES == ["CHILD_OF", "LINKS_TO", "MENTIONS", "TAGGED_AS"]


# --- get_concept -------------------------------------------------------------


@pytest.fixture
def bundle(cfg):
    b = cfg.bundle_dir
    (b / "runbook").mkdir(parents=True)
    (b / "runbook" / "restart.md").write_text(
        "---\ntype: Runbook\ntitle: Restart\ndescription: How to restart.\n"
        "sensitivity: work\nsource_hash: aa11\n---\n\n# Steps\n\nStop, wait, start.\n"
    )
    return cfg


def test_get_concept_returns_frontmatter_and_markdown(bundle):
    out = _impl_get_concept(bundle, "runbook/restart")
    assert out["concept_id"] == "runbook/restart"
    assert out["frontmatter"]["type"] == "Runbook"
    assert out["markdown"].startswith("---")
    assert "Stop, wait, start." in out["markdown"]


def test_get_concept_accepts_an_id_with_the_md_suffix(bundle):
    assert _impl_get_concept(bundle, "runbook/restart.md")["concept_id"] == "runbook/restart"


def test_get_concept_reports_a_missing_concept(bundle):
    out = _impl_get_concept(bundle, "runbook/nope")
    assert "error" in out and "hint" in out


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../../etc/passwd",
        "/etc/passwd",
        "runbook/../../../../etc/passwd",
        "..%2F..%2Fetc%2Fpasswd",
    ],
)
def test_get_concept_refuses_to_escape_the_bundle(bundle, hostile):
    """The concept_id becomes a path, so traversal must be impossible."""
    out = _impl_get_concept(bundle, hostile)
    assert "error" in out
    assert "markdown" not in out


# --- search ------------------------------------------------------------------


def test_search_clamps_the_limit(bundle, monkeypatch):
    seen = {}

    def fake_search(cfg, query, limit, **kw):
        seen["limit"] = limit
        return [], {"total_ms": 1.0}

    monkeypatch.setattr("kb.retrieve.search", fake_search)
    _impl_search(bundle, "x", limit=9999)
    assert seen["limit"] == 50
    _impl_search(bundle, "x", limit=0)
    assert seen["limit"] == 1


def test_search_reports_a_missing_index_instead_of_raising(bundle, monkeypatch):
    monkeypatch.setattr(
        "kb.retrieve.search",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("no vector table")),
    )
    out = _impl_search(bundle, "anything")
    assert out["results"] == []
    assert "kb index" in out["hint"]


def test_search_never_raises_when_ollama_is_unreachable(bundle):
    """A tool must answer with an actionable error, not crash the session."""
    out = _impl_search(bundle, "anything")   # test config names a model that does not exist
    assert out["results"] == []
    assert "error" in out
    assert "ollama" in out["hint"].lower()


def test_search_survives_any_backend_failure(bundle, monkeypatch):
    monkeypatch.setattr(
        "kb.retrieve.search",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("backend exploded")),
    )
    out = _impl_search(bundle, "anything")
    assert "backend exploded" in out["error"]
    assert out["results"] == []


def test_search_shapes_every_result(bundle, monkeypatch):
    from kb.retrieve import Hit

    hit = Hit(
        "runbook/restart#0", "runbook/restart", "Restart", "Steps",
        "Stop, wait, start.", "local", "Runbook", "work",
        vector_rank=1, bm25_rank=3, rerank=0.87,
    )
    hit.file_path = "/Users/example/Documents/a b.pdf"
    monkeypatch.setattr(
        "kb.retrieve.search", lambda *a, **k: ([hit], {"total_ms": 42.0})
    )
    out = _impl_search(bundle, "restart")
    assert out["count"] == 1 and out["took_ms"] == 42.0
    r = out["results"][0]
    assert r["rank"] == 1
    assert r["score"] == 0.87
    assert r["found_by"] == ["vector#1", "bm25#3"]
    assert r["file_path"] == "/Users/example/Documents/a b.pdf"
    assert set(r) == {
        "rank", "concept_id", "title", "heading", "concept_type", "sensitivity",
        "source", "score", "found_by", "file_path", "text",
    }


def test_search_result_is_json_serialisable(bundle, monkeypatch):
    monkeypatch.setattr("kb.retrieve.search", lambda *a, **k: ([], {"total_ms": 1.0}))
    json.dumps(_impl_search(bundle, "x"))  # must not raise


# --- traverse ----------------------------------------------------------------


def test_traverse_rejects_unknown_edge_types(bundle):
    out = _impl_traverse(bundle, "runbook/restart", edge_types=["FRIENDS_WITH"])
    assert "error" in out
    assert out["known"] == EDGE_TYPES


def test_traverse_accepts_lowercase_edge_names(bundle, monkeypatch):
    import pandas as pd

    monkeypatch.setattr("kb.graph.query", lambda cfg, q: pd.DataFrame(
        columns=["concept_id", "title", "concept_type", "sensitivity", "via"]))
    out = _impl_traverse(bundle, "runbook/restart", edge_types=["links_to"])
    assert out["edge_types"] == ["LINKS_TO"]


def test_traverse_reports_a_missing_graph(bundle):
    out = _impl_traverse(bundle, "runbook/restart")
    assert "error" in out and "kb graph" in out["hint"]


def test_traverse_merges_reasons_for_one_neighbour(bundle, monkeypatch):
    import pandas as pd

    def fake_query(cfg, cypher):
        via = "Acme Corp" if "Entity" in cypher else ""
        return pd.DataFrame(
            [{"concept_id": "report/x", "title": "X", "concept_type": "Report",
              "sensitivity": "work", "via": via}]
        )

    monkeypatch.setattr("kb.graph.query", fake_query)
    out = _impl_traverse(
        bundle, "runbook/restart", edge_types=["LINKS_TO", "MENTIONS"]
    )
    assert out["count"] == 1
    reasons = out["neighbours"][0]["reasons"]
    assert {r["edge"] for r in reasons} == {"LINKS_TO", "MENTIONS"}
    assert any(r["via"] == "Acme Corp" for r in reasons)


def test_traverse_clamps_hops_and_limit(bundle, monkeypatch):
    import pandas as pd

    monkeypatch.setattr("kb.graph.query", lambda cfg, q: pd.DataFrame(
        columns=["concept_id", "title", "concept_type", "sensitivity", "via"]))
    assert _impl_traverse(bundle, "a", hops=99)["hops"] == 2
    assert _impl_traverse(bundle, "a", hops=0)["hops"] == 1


def test_traverse_escapes_quotes_in_the_concept_id(bundle, monkeypatch):
    captured = {}
    import pandas as pd

    def fake_query(cfg, cypher):
        captured["cypher"] = cypher
        return pd.DataFrame(
            columns=["concept_id", "title", "concept_type", "sensitivity", "via"])

    monkeypatch.setattr("kb.graph.query", fake_query)
    _impl_traverse(bundle, "runbook/o'brien", edge_types=["LINKS_TO"])
    assert "o''brien" in captured["cypher"]


# --- server wiring -----------------------------------------------------------


def test_build_server_registers_three_tools(bundle):
    server = build_server(bundle)
    assert server.name == "okf-kb"


# --- stdout hygiene, against a real subprocess -------------------------------


@pytest.mark.slow
def test_stdout_carries_only_json_rpc():
    """A single stray print would corrupt the protocol. Assert it, don't assume."""
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
    proc = subprocess.Popen(
        [sys.executable, "-m", "kb.mcp_server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, cwd=str(REPO), env=env,
    )
    try:
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "1"}},
        }) + "\n")
        proc.stdin.flush()
        first = proc.stdout.readline()

        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n")
        proc.stdin.flush()
        second = proc.stdout.readline()
        proc.stdin.close()
        rest = proc.stdout.read()
    finally:
        proc.terminate()
        proc.wait(timeout=30)

    lines = [l for l in (first, second, *rest.splitlines()) if l.strip()]
    assert lines, "server produced no stdout"
    for line in lines:
        json.loads(line)  # every stdout line must be valid JSON

    payload = json.loads(second)
    names = {t["name"] for t in payload["result"]["tools"]}
    assert names == {"search_knowledge", "get_concept", "traverse"}


def test_the_search_tool_does_not_run_the_pipeline_for_an_empty_query(cfg, monkeypatch):
    """Third entry point to the same feature, third time this guard was missing.

    The CLI had it fixed this morning, the web endpoint has had it since Task 6.
    Without it, `search_knowledge(query="")` embeds an empty string through
    Ollama and spends reranker GPU on the result.
    """
    import kb.retrieve

    def refuse(*a, **k):
        raise AssertionError("ran the retrieval pipeline for an empty query")

    monkeypatch.setattr(kb.retrieve, "search", refuse)

    for q in ("", "   ", "\n\t"):
        out = _impl_search(cfg, q)
        assert out["results"] == []
        assert "error" not in out, out


def test_the_search_tool_still_answers_a_real_query(bundle, monkeypatch):
    """The guard must not swallow real work: a non-empty query still reaches
    the pipeline, even if this fixture has no index behind it."""
    called = {}

    def spy(cfg, q, **k):
        called["q"] = q
        return [], {"total_ms": 1.0}

    monkeypatch.setattr("kb.retrieve.search", spy)
    _impl_search(bundle, "widget service")
    assert called.get("q") == "widget service"
