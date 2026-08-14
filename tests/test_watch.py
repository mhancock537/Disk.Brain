"""Incremental updates: scope gating, debounce, inline processing, deprecation."""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

import pytest

from kb.bundle import deprecate_missing, undeprecate, write_bundle
from kb.enrich import Record, run_enrich
from kb.extract.runner import run_extract
from kb.index import delete_from_indexes, update_index
from kb.manifest import DenyList, Manifest, scan
from kb.okf import parse
from kb.watch import ChangeQueue, Pending, in_scope, process_path


# --- the change queue --------------------------------------------------------


def test_queue_debounces_repeated_events():
    q = ChangeQueue(debounce_seconds=10.0, max_size=100)
    q.add(Path("/a/b.md"))
    q.add(Path("/a/b.md"))
    assert len(q) == 1
    assert q.take_settled() == []      # not quiet long enough yet


def test_queue_releases_once_quiet():
    q = ChangeQueue(debounce_seconds=0.01, max_size=100)
    q.add(Path("/a/b.md"))
    time.sleep(0.05)
    settled = q.take_settled()
    assert [p.path for p in settled] == [Path("/a/b.md")]
    assert len(q) == 0                 # taken items leave the queue


def test_queue_caps_and_counts_drops():
    q = ChangeQueue(debounce_seconds=10.0, max_size=2)
    for i in range(5):
        q.add(Path(f"/a/{i}.md"))
    assert len(q) == 2
    assert q.dropped == 3


def test_queue_updates_an_existing_entry_without_growing():
    q = ChangeQueue(debounce_seconds=10.0, max_size=1)
    q.add(Path("/a/b.md"))
    q.add(Path("/a/b.md"), deleted=True)
    assert len(q) == 1 and q.dropped == 0


# --- scope gating ------------------------------------------------------------


def test_in_scope_accepts_a_document_under_a_root(cfg, corpus):
    deny = DenyList(cfg.deny_globs)
    assert in_scope(cfg, deny, corpus / "notes" / "note1.md")


def test_in_scope_rejects_a_denied_path(cfg, corpus):
    deny = DenyList(cfg.deny_globs)
    assert not in_scope(cfg, deny, corpus / "node_modules" / "pkg" / "index.md")


def test_in_scope_rejects_an_unroutable_extension(cfg, corpus):
    deny = DenyList(cfg.deny_globs)
    assert not in_scope(cfg, deny, corpus / "image.png")
    assert not in_scope(cfg, deny, corpus / "code" / "app.py")   # code is off


def test_in_scope_rejects_a_path_outside_every_root(cfg, tmp_path):
    """Watching is wider than indexing, on purpose."""
    deny = DenyList(cfg.deny_globs)
    outside = tmp_path / "elsewhere" / "stray.md"
    outside.parent.mkdir(parents=True)
    outside.write_text("text")
    assert not in_scope(cfg, deny, outside)


def test_require_configured_root_can_be_turned_off(cfg, tmp_path):
    deny = DenyList(cfg.deny_globs)
    outside = tmp_path / "elsewhere" / "stray.md"
    outside.parent.mkdir(parents=True)
    outside.write_text("text")
    wide = replace(cfg, watch=replace(cfg.watch, require_configured_root=False))
    assert in_scope(wide, deny, outside)


# --- inline processing -------------------------------------------------------


@pytest.fixture
def scanned(cfg, tmp_path):
    mf = Manifest(tmp_path / "m.db")
    scan(cfg, mf, do_hash=True)
    run_extract(cfg, mf, show_progress=False)
    yield mf
    mf.close()


def test_process_new_file_hashes_and_extracts(cfg, corpus, scanned):
    new = corpus / "notes" / "brand-new.md"
    new.write_text("# Fresh\n\nA brand new note about Acme Corp.\n")
    run = scanned.start_run("test")
    assert process_path(cfg, scanned, Pending(new, 0.0), run) == "extracted"

    row = scanned.conn.execute(
        "SELECT hash, extract_status, extract_path FROM files WHERE path = ?",
        (str(new),),
    ).fetchone()
    assert row["hash"] and row["extract_status"] == "ok"
    assert Path(row["extract_path"]).is_file()


def test_process_unchanged_file_does_no_work(cfg, corpus, scanned):
    existing = corpus / "notes" / "note1.md"
    run = scanned.start_run("test")
    assert process_path(cfg, scanned, Pending(existing, 0.0), run) == "unchanged"


def test_process_changed_file_reextracts(cfg, corpus, scanned):
    existing = corpus / "notes" / "note1.md"
    before = scanned.conn.execute(
        "SELECT hash FROM files WHERE path = ?", (str(existing),)
    ).fetchone()["hash"]
    existing.write_text("# Note 1\n\nCompletely different content now.\n")
    run = scanned.start_run("test")
    assert process_path(cfg, scanned, Pending(existing, 0.0), run) == "extracted"
    after = scanned.conn.execute(
        "SELECT hash FROM files WHERE path = ?", (str(existing),)
    ).fetchone()["hash"]
    assert after != before


def test_process_deleted_file_marks_it_missing(cfg, corpus, scanned):
    gone = corpus / "notes" / "note2.md"
    gone.unlink()
    run = scanned.start_run("test")
    assert process_path(cfg, scanned, Pending(gone, 0.0), run) == "deleted"
    row = scanned.conn.execute(
        "SELECT scan_status FROM files WHERE path = ?", (str(gone),)
    ).fetchone()
    assert row["scan_status"] == "missing"


def test_process_ignores_out_of_scope_paths(cfg, corpus, scanned):
    run = scanned.start_run("test")
    junk = corpus / "node_modules" / "pkg" / "index.md"
    assert process_path(cfg, scanned, Pending(junk, 0.0), run) == "ignored"


def test_process_recognises_a_moved_file_by_hash(cfg, corpus, scanned):
    """A move keeps the hash, so nothing is re-extracted or re-enriched."""
    src = corpus / "notes" / "note3.md"
    dest = corpus / "notes" / "note3-renamed.md"
    src.rename(dest)
    run = scanned.start_run("test")
    assert process_path(cfg, scanned, Pending(dest, 0.0), run) == "known_hash"


def test_process_never_raises_on_a_vanished_path(cfg, scanned, tmp_path):
    run = scanned.start_run("test")
    assert process_path(cfg, scanned, Pending(tmp_path / "nope.md", 0.0), run) in (
        "deleted", "ignored",
    )


# --- deprecation -------------------------------------------------------------


@pytest.fixture
def bundled(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr("kb.enrich.check_model", lambda c: (True, "stub"))
    state = {"i": 0}

    def fake(c, filename, path, text):
        state["i"] += 1
        return Record(
            title=f"Concept {state['i']}", description=f"Number {state['i']}.",
            concept_type="Reference", tags=["alpha"], entities=[],
            sensitivity="work",
        )

    monkeypatch.setattr("kb.enrich.enrich_one", fake)
    mf = Manifest(tmp_path / "m.db")
    scan(cfg, mf, do_hash=True)
    run_extract(cfg, mf, show_progress=False)
    run_enrich(cfg, mf, show_progress=False)
    write_bundle(cfg, mf, show_progress=False)
    yield mf
    mf.close()


def test_deprecate_marks_concepts_whose_source_is_gone(cfg, corpus, bundled):
    cid = bundled.conn.execute(
        "SELECT concept_id FROM concepts WHERE enrich_status='ok' LIMIT 1"
    ).fetchone()["concept_id"]
    source_hash = bundled.conn.execute(
        "SELECT source_hash FROM concepts WHERE concept_id = ?", (cid,)
    ).fetchone()["source_hash"]
    bundled.conn.execute(
        "UPDATE files SET scan_status='missing' WHERE hash = ?", (source_hash,)
    )
    bundled.commit()

    out = deprecate_missing(cfg, bundled)
    assert cid in out["concept_ids"]

    doc = parse((cfg.bundle_dir / f"{cid}.md").read_text())
    assert doc.frontmatter["status"] == "deprecated"
    assert doc.frontmatter["deprecated_at"]


def test_deprecated_concept_file_is_never_deleted(cfg, corpus, bundled):
    cid = bundled.conn.execute(
        "SELECT concept_id FROM concepts WHERE enrich_status='ok' LIMIT 1"
    ).fetchone()["concept_id"]
    bundled.conn.execute("UPDATE files SET scan_status='missing'")
    bundled.commit()
    deprecate_missing(cfg, bundled)
    assert (cfg.bundle_dir / f"{cid}.md").is_file()


def test_deprecation_is_idempotent(cfg, bundled):
    bundled.conn.execute("UPDATE files SET scan_status='missing'")
    bundled.commit()
    first = deprecate_missing(cfg, bundled)["deprecated"]
    second = deprecate_missing(cfg, bundled)["deprecated"]
    assert first > 0 and second == 0


def test_deprecation_writes_an_iso_log_entry(cfg, bundled):
    bundled.conn.execute("UPDATE files SET scan_status='missing'")
    bundled.commit()
    deprecate_missing(cfg, bundled)
    text = (cfg.bundle_dir / "log.md").read_text()
    assert "**Deprecation**" in text
    import re

    assert re.search(r"^## \d{4}-\d{2}-\d{2}$", text, re.M)


def test_undeprecate_revives_a_restored_file(cfg, bundled):
    bundled.conn.execute("UPDATE files SET scan_status='missing'")
    bundled.commit()
    out = deprecate_missing(cfg, bundled)
    revived = undeprecate(cfg, out["concept_ids"])
    assert revived == out["deprecated"]
    cid = out["concept_ids"][0]
    doc = parse((cfg.bundle_dir / f"{cid}.md").read_text())
    assert doc.frontmatter["status"] == "stable"
    assert "deprecated_at" not in doc.frontmatter


def test_deprecated_concepts_leave_the_indexes(cfg, bundled, monkeypatch):
    """A deprecated concept stays in the bundle and out of retrieval."""
    import kb.embed as embed_mod
    from test_index import StubEmbedder

    monkeypatch.setattr(embed_mod, "check_embed_model", lambda c: (True, "stub"))
    monkeypatch.setattr(embed_mod, "Embedder", StubEmbedder)
    embed_mod._TABLE_CACHE.clear()

    from kb.index import run_index

    run_index(cfg, show_progress=False)
    cid = bundled.conn.execute(
        "SELECT concept_id FROM concepts WHERE enrich_status='ok' LIMIT 1"
    ).fetchone()["concept_id"]

    bundled.conn.execute("UPDATE files SET scan_status='missing'")
    bundled.commit()
    deprecate_missing(cfg, bundled)

    stats = update_index(cfg, [cid])
    assert stats["dropped"] == 1        # removed, not re-added
    assert stats["added"]["fts"] == 0


# --- incremental index -------------------------------------------------------


def test_update_index_touches_only_the_named_concepts(cfg, bundled, monkeypatch):
    import kb.embed as embed_mod
    from test_index import StubEmbedder

    monkeypatch.setattr(embed_mod, "check_embed_model", lambda c: (True, "stub"))
    monkeypatch.setattr(embed_mod, "Embedder", StubEmbedder)
    embed_mod._TABLE_CACHE.clear()

    from kb.index import run_index

    run_index(cfg, show_progress=False)
    total_before = embed_mod.open_table(cfg).count_rows()

    cid = bundled.conn.execute(
        "SELECT concept_id FROM concepts WHERE enrich_status='ok' LIMIT 1"
    ).fetchone()["concept_id"]
    stats = update_index(cfg, [cid])

    assert stats["concepts"] == 1
    assert stats["removed"]["fts"] == stats["added"]["fts"]
    assert embed_mod.open_table(cfg).count_rows() == total_before


def test_delete_from_indexes_is_safe_when_nothing_exists(cfg):
    assert delete_from_indexes(cfg, ["a/b"]) == {"lance": 0, "fts": 0}


def test_update_index_with_no_ids_is_a_noop(cfg):
    assert update_index(cfg, [])["concepts"] == 0


# --- the drain cap -----------------------------------------------------------


def test_drain_limit_zero_enriches_nothing(cfg, bundled, monkeypatch):
    """`--limit 0` must mean zero, not fall through to the configured cap.

    Regression: `limit or cfg.drain.max_documents` treated 0 as absent and
    started a 200-document enrichment run that had been explicitly declined.
    """
    from kb.watch import run_drain

    called = {"n": 0}

    def counting_enrich(*a, **k):
        called["n"] += 1
        return {"ok": 0, "failed": 0}

    monkeypatch.setattr("kb.enrich.run_enrich", counting_enrich)
    stats = run_drain(cfg, limit=0, show_progress=False)
    assert called["n"] == 0
    assert stats["cap"] == 0


def test_drain_none_limit_uses_the_configured_cap(cfg, bundled, monkeypatch):
    from kb.watch import run_drain

    seen = {}

    def counting_enrich(cfg_, mf, limit=None, **k):
        seen["limit"] = limit
        return {"ok": 0, "failed": 0}

    monkeypatch.setattr("kb.enrich.run_enrich", counting_enrich)
    run_drain(cfg, limit=None, show_progress=False)
    assert seen.get("limit") in (None, cfg.drain.max_documents)
