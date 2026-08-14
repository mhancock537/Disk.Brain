"""Shared fixtures. The corpus fixture is a real 20-file tree on disk."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from kb.config import (
    BundleConfig,
    ChunkConfig,
    Config,
    EmbedConfig,
    EnrichConfig,
    OcrConfig,
    DrainConfig,
    RetrieveConfig,
    Root,
    WatchConfig,
)

FIXTURE_FILE_COUNT = 20


def make_config(tmp_path: Path, corpus: Path, **overrides) -> Config:
    defaults = dict(
        root_dir=tmp_path,
        roots=[Root(path=corpus, enabled=True, sensitivity="personal")],
        deny_globs=["node_modules", ".git", "*.dmg", "*.zip"],
        max_file_bytes=1024 * 64,
        follow_symlinks=False,
        same_device_only=True,
        include_source_code=False,
        extract_out_dir=tmp_path / "data" / "extracted",
        extract_min_chars=5,
        ocr=OcrConfig(
            enabled=False,
            chars_per_page_threshold=100,
            scanned_page_fraction=0.6,
            sample_pages=8,
            max_pages=40,
            dpi=200,
            images=False,
        ),
        enrich=EnrichConfig(
            model="test-model",
            prompt_words=200,
            temperature=0.2,
            num_ctx=4096,
            timeout_seconds=60,
            max_tags=6,
            max_entities=12,
            max_attempts=2,
            types={
                "Runbook": "a procedure",
                "Reference": "lookup material",
                "Report": "analysis",
                "Other": "none of the above",
            },
            sensitivity_default="personal",
            work_globs=[],
            personal_globs=[],
        ),
        chunk=ChunkConfig(max_tokens=800, overlap_ratio=0.15),
        embed=EmbedConfig(
            model="test-embed", batch_size=8, metric="cosine", use_ann=False
        ),
        retrieve=RetrieveConfig(
            vector_top_k=50,
            bm25_top_k=50,
            rrf_k=60,
            fuse_top_n=20,
            graph_hops=1,
            graph_neighbour_chunks=2,
            rerank_max_candidates=30,
            limit=15,
            rerank_enabled=False,   # tests stub the reranker; no model download
            rerank_model="stub-reranker",
            rerank_instruction="Judge the document.",
            rerank_batch_size=8,
            rerank_doc_chars=700,
            weak_score=0.20,
        ),
        watch=WatchConfig(
            root=corpus,
            require_configured_root=True,
            debounce_seconds=0.05,
            max_queue=100,
        ),
        drain=DrainConfig(max_documents=10, rebuild_graph=False),
        bundle=BundleConfig(
            max_related=8,
            link_rarity_ceiling=60,
            min_link_score=0.35,
        ),
        log_level="WARNING",
        log_format="text",
    )
    defaults.update(overrides)
    return Config(**defaults)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """A 20-file tree covering every branch the crawler and router can take."""
    root = tmp_path / "corpus"
    (root / "notes").mkdir(parents=True)
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "code").mkdir()

    # 1-6: markdown prose
    for i in range(1, 7):
        (root / "notes" / f"note{i}.md").write_text(
            textwrap.dedent(f"""\
                # Note {i}

                Body text for note {i}. Mentions Acme Corp and project Falcon.
                """),
            encoding="utf-8",
        )

    # 7-8: identical content at two paths, for the dedup path
    dup = "# Duplicate\n\nThe very same bytes in two places.\n"
    (root / "notes" / "dup_a.md").write_text(dup, encoding="utf-8")
    (root / "dup_b.md").write_text(dup, encoding="utf-8")

    # 9-10: plain text and csv
    (root / "readme.txt").write_text("Plain text file with content.\n", encoding="utf-8")
    (root / "table.csv").write_text("a,b,c\n1,2,3\n4,5,6\n", encoding="utf-8")

    # 11: html, goes through the office chain
    (root / "page.html").write_text(
        "<html><body><h1>Title</h1><p>Hello from HTML.</p></body></html>",
        encoding="utf-8",
    )

    # 12: latin-1, exercises the encoding ladder
    (root / "latin.txt").write_bytes("Caf\xe9 na\xefve r\xe9sum\xe9.\n".encode("latin-1"))

    # 13: empty file
    (root / "empty.md").write_text("", encoding="utf-8")

    # 14: real PDF with a text layer
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Invoice 4471 for Acme Corp, total 1200 USD.")
    doc.save(root / "invoice.pdf")
    doc.close()

    # 15: real xlsx
    import xlsxwriter

    wb = xlsxwriter.Workbook(str(root / "sheet.xlsx"))
    ws = wb.add_worksheet()
    for r, row in enumerate([["name", "amount"], ["Acme", 100], ["Globex", 200]]):
        for c, v in enumerate(row):
            ws.write(r, c, v)
    wb.close()

    # 16: source code, excluded unless include_source_code is on
    (root / "code" / "app.py").write_text("def main():\n    return 42\n", encoding="utf-8")

    # 17: png, no extractor route with ocr.images off
    (root / "image.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    )

    # 18: inside the denylist
    (root / "node_modules" / "pkg" / "index.md").write_text("junk\n", encoding="utf-8")

    # 19: denied by extension glob
    (root / "archive.zip").write_bytes(b"PK\x03\x04" + b"\x00" * 32)

    # 20: over max_file_bytes (64 KB in the test config)
    (root / "big.md").write_text("x" * (70 * 1024), encoding="utf-8")

    return root


@pytest.fixture
def cfg(tmp_path: Path, corpus: Path) -> Config:
    return make_config(tmp_path, corpus)
