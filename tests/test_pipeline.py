"""End to end over the 20-file fixture: scan, hash, extract, report."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from rich.console import Console

from kb.extract.runner import run_extract
from kb.manifest import Manifest, scan
from kb.report import error_class, extract_report, scan_report


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
