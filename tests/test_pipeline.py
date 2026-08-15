"""End to end over the 20-file fixture: scan, hash, extract, report."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from rich.console import Console
from typer.testing import CliRunner

from kb.bundle import write_bundle
from kb.cli import app
from kb.config import Root
from kb.enrich import Record, run_enrich
from kb.extract.runner import run_extract
from kb.graph import build_graph
from kb.index import run_index
from kb.manifest import Manifest, scan
from kb.report import error_class, extract_report, scan_report
from kb.retrieve import search


def test_full_pipeline(cfg, tmp_path):
    with Manifest(tmp_path / "m.db") as mf:
        scan_stats = scan(cfg, mf, do_hash=True)
        assert scan_stats["hash_failed"] == 0

        stats = run_extract(cfg, mf, show_progress=False)
        assert stats["ok"] >= 10
        assert stats["failed"] == 0
        assert stats["total_words"] > 0

        # Extracted markdown landed on disk, sharded, one file per hash.
        written = list(cfg.extract_out_dir.rglob("*.md"))
        assert len(written) == stats["ok"]
        for f in written:
            assert f.parent.name == f.stem[:2]

        # Duplicate pair shares one extraction but both rows say ok.
        dup_rows = mf.conn.execute(
            "SELECT extract_path FROM files WHERE path LIKE '%dup_%'"
        ).fetchall()
        assert len(dup_rows) == 2
        assert dup_rows[0]["extract_path"] == dup_rows[1]["extract_path"]

        # The PDF's text survived the round trip.
        pdf = mf.conn.execute(
            "SELECT extract_path FROM files WHERE path LIKE '%invoice.pdf'"
        ).fetchone()
        assert "Acme Corp" in Path(pdf["extract_path"]).read_text()

        # Empty source recorded as empty, not failed.
        empty = mf.conn.execute(
            "SELECT extract_status FROM files WHERE path LIKE '%empty.md'"
        ).fetchone()
        assert empty["extract_status"] == "empty"


def test_extract_resumes_after_interruption(cfg, tmp_path):
    with Manifest(tmp_path / "m.db") as mf:
        scan(cfg, mf, do_hash=True)
        total = len(mf.pending_extractions())

        first = run_extract(cfg, mf, limit=3, show_progress=False)
        done = sum(first[k] for k in ("ok", "empty", "failed", "skipped"))
        assert done == 3
        assert len(mf.pending_extractions()) == total - 3

        run_extract(cfg, mf, show_progress=False)
        assert mf.pending_extractions() == []


def test_extract_limit_and_ext_filter(cfg, tmp_path):
    with Manifest(tmp_path / "m.db") as mf:
        scan(cfg, mf, do_hash=True)
        stats = run_extract(cfg, mf, only_ext="pdf", show_progress=False)
        assert stats["ok"] == 1
        remaining = {r["ext"] for r in mf.pending_extractions()}
        assert ".pdf" not in remaining


def test_reports_render_without_error(cfg, tmp_path):
    console = Console(file=open(tmp_path / "out.txt", "w"), width=120)
    with Manifest(tmp_path / "m.db") as mf:
        scan(cfg, mf, do_hash=True)
        run_extract(cfg, mf, show_progress=False)
        scan_report(cfg, mf, console)
        extract_report(cfg, mf, console)
    text = (tmp_path / "out.txt").read_text()
    assert "Scan status" in text
    assert "Total extracted words" in text


def test_source_code_toggle_changes_corpus_size(cfg, tmp_path):
    with Manifest(tmp_path / "a.db") as mf:
        without = scan(cfg, mf, do_hash=False)["included"]
    with Manifest(tmp_path / "b.db") as mf:
        with_code = scan(replace(cfg, include_source_code=True), mf, do_hash=False)[
            "included"
        ]
    assert with_code > without


def test_error_class_collapses_variants():
    a = error_class("pandoc exit 3: /Users/example/a b/c.epub is broken at line 42")
    b = error_class("pandoc exit 9: /Users/example/z/other.epub is broken at line 7")
    assert a == b
    assert error_class(None) == "unknown"


def test_cockpit_card_reaches_search_without_changing_lessons(
    cfg, tmp_path, monkeypatch
):
    import kb.embed as embed_mod
    import kb.retrieve as retrieve_mod
    from test_index import StubEmbedder

    vault = tmp_path / "vault"
    vault.mkdir()
    runner = CliRunner()
    initialized = runner.invoke(app, ["cockpit", "init", "--vault", str(vault)])
    assert initialized.exit_code == 0
    lesson = tmp_path / "lessons.md"
    lesson.write_bytes(b"# Existing Lessons\n\nKeep these bytes exact.\n")
    lesson_before = lesson.read_bytes()

    card_input = tmp_path / "card.json"
    card_input.write_text(
        json.dumps(
            {
                "id": "codex-cockpit-proof",
                "project": "disk-brain",
                "occurred_at": "2026-08-14T14:30:00-05:00",
                "tool": "Codex",
                "source_ref": "codex://task/cockpit-proof",
                "status": "completed",
                "summary": "Quartz cockpit semaphore reached the searchable corpus.",
                "decisions": ["Keep session cards immutable."],
                "artifacts": ["src/kb/cockpit.py"],
                "open_loops": [],
                "lesson_keys": ["cockpit-capture"],
            }
        )
    )
    capture = runner.invoke(
        app,
        ["cockpit", "capture", "--vault", str(vault), "--input", str(card_input)],
    )
    assert capture.exit_code == 0
    captured_path = Path(json.loads(capture.stdout)["path"])
    cockpit_cfg = replace(
        cfg,
        roots=[Root(path=vault, enabled=True, sensitivity="personal")],
        watch=replace(cfg.watch, root=vault),
    )

    monkeypatch.setattr("kb.enrich.check_model", lambda c: (True, "stub"))

    def fake_enrich(c, filename, path, text):
        is_card = "Quartz cockpit semaphore" in text
        return Record(
            title="Cockpit Search Proof" if is_card else Path(filename).stem,
            description=(
                "Quartz cockpit semaphore session card."
                if is_card
                else "Obsidian cockpit scaffold note."
            ),
            concept_type="Reference",
            tags=["cockpit"],
            entities=[],
            sensitivity="personal",
        )

    monkeypatch.setattr("kb.enrich.enrich_one", fake_enrich)
    monkeypatch.setattr(embed_mod, "check_embed_model", lambda c: (True, "stub"))
    monkeypatch.setattr(embed_mod, "Embedder", StubEmbedder)
    monkeypatch.setattr(retrieve_mod, "Embedder", StubEmbedder)
    embed_mod._TABLE_CACHE.clear()

    with Manifest(cockpit_cfg.manifest_path) as mf:
        scan_stats = scan(cockpit_cfg, mf, do_hash=True)
        extract_stats = run_extract(cockpit_cfg, mf, show_progress=False)
        enrich_stats = run_enrich(cockpit_cfg, mf, show_progress=False)
        bundle_stats, report = write_bundle(cockpit_cfg, mf, show_progress=False)

    index_stats = run_index(cockpit_cfg, show_progress=False)
    graph_stats = build_graph(cockpit_cfg)
    hits, _ = search(cockpit_cfg, "quartz cockpit semaphore")

    assert scan_stats["included"] == 3
    assert extract_stats["failed"] == 0
    assert enrich_stats["ok"] == 3
    assert bundle_stats["concepts"] == 3 and report.ok
    assert index_stats["concepts"] == 3
    assert graph_stats["nodes"]["Concept"] == 3
    assert hits and hits[0].title == "Cockpit Search Proof"
    assert Path(hits[0].file_path) == captured_path
    assert "codex://task/cockpit-proof" in captured_path.read_text()
    assert lesson.read_bytes() == lesson_before
