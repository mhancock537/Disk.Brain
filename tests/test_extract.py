from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from kb.extract import extract_file, route
from kb.extract.office import extract_office
from kb.extract.pdf import _sample_indices, extract_pdf
from kb.extract.plain import extract_plain


# --- routing -----------------------------------------------------------------


@pytest.mark.parametrize(
    "ext,mime,expected",
    [
        (".pdf", None, "pdf"),
        ("", "application/pdf", "pdf"),
        (".docx", None, "office"),
        (".epub", None, "office"),
        (".md", None, "plain"),
        (".csv", None, "plain"),
        (".py", None, "skip"),      # include_source_code is off in the fixture
        (".png", None, "skip"),     # ocr.images is off
        (".xyz", None, "skip"),
    ],
)
def test_route(cfg, ext, mime, expected):
    assert route(ext, mime, cfg) == expected


def test_route_includes_code_when_enabled(cfg):
    on = replace(cfg, include_source_code=True)
    assert route(".py", None, on) == "plain"
    assert route(".md", None, on) == "plain"


def test_route_includes_images_when_ocr_images_on(cfg):
    on = replace(cfg, ocr=replace(cfg.ocr, enabled=True, images=True))
    assert route(".png", None, on) == "image"


# --- plain -------------------------------------------------------------------


def test_plain_utf8(tmp_path):
    p = tmp_path / "a.md"
    p.write_text("# Hello\n\nWorld\n", encoding="utf-8")
    r = extract_plain(p)
    assert r.status == "ok" and r.word_count == 3
    assert r.meta["encoding"] == "utf-8"


def test_plain_latin1_fallback(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes("Café naïve\n".encode("latin-1"))
    r = extract_plain(p)
    assert r.status == "ok"
    assert "Caf" in r.text


def test_plain_empty_file(tmp_path):
    p = tmp_path / "a.md"
    p.write_text("   \n")
    assert extract_plain(p).status == "empty"


def test_plain_rejects_binary(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(bytes(range(1, 256)) * 40)
    r = extract_plain(p)
    assert r.status == "failed"
    assert "binary" in (r.error or "")


def test_plain_normalises_crlf(tmp_path):
    p = tmp_path / "a.md"
    p.write_bytes(b"one\r\ntwo\r\n")
    assert extract_plain(p).text == "one\ntwo\n"


# --- pdf ---------------------------------------------------------------------


def test_sample_indices_span_the_document():
    assert _sample_indices(3, 8) == [0, 1, 2]
    idx = _sample_indices(100, 8)
    assert len(idx) == 8 and idx[0] == 0 and idx[-1] < 100


def test_pdf_with_text_layer(cfg, corpus):
    r = extract_pdf(corpus / "invoice.pdf", cfg)
    assert r.status == "ok"
    assert "Acme Corp" in r.text
    assert r.engine == "pymupdf"
    assert r.meta["route"] == "text"
    assert r.meta["pages"] == 1


def test_pdf_with_no_text_and_ocr_disabled(cfg, tmp_path):
    import pymupdf

    doc = pymupdf.open()
    doc.new_page()
    out = tmp_path / "blank.pdf"
    doc.save(out)
    doc.close()

    r = extract_pdf(out, cfg)  # cfg has ocr.enabled = False
    assert r.status == "empty"
    assert "OCR disabled" in (r.error or "")
    assert r.meta["thin_pages"] == 1


def test_pdf_scanned_detection_routes_to_ocr(cfg, tmp_path, monkeypatch):
    import pymupdf

    doc = pymupdf.open()
    doc.new_page()
    out = tmp_path / "scan.pdf"
    doc.save(out)
    doc.close()

    on = replace(cfg, ocr=replace(cfg.ocr, enabled=True))
    called = {}

    def fake_ocr(path, c, page_numbers=None):
        from kb.extract import ExtractResult

        called["hit"] = True
        return ExtractResult(status="ok", text="ocr text", engine="ocrmac/vision")

    monkeypatch.setattr("kb.extract.ocr.ocr_pdf", fake_ocr)
    r = extract_pdf(out, on)
    assert called.get("hit") is True
    assert r.meta["route"] == "ocr-full"
    assert r.text == "ocr text"


# --- office ------------------------------------------------------------------


def test_office_html(corpus):
    r = extract_office(corpus / "page.html", ".html")
    assert r.status == "ok"
    assert "Hello from HTML" in r.text


def test_office_xlsx(corpus):
    r = extract_office(corpus / "sheet.xlsx", ".xlsx")
    assert r.status == "ok"
    assert "Globex" in r.text


def test_office_falls_through_to_next_engine(corpus, monkeypatch):
    """A failing first engine must not fail the file."""
    import kb.extract.office as office

    def boom(path, ext):
        raise RuntimeError("engine exploded")

    monkeypatch.setitem(office.ENGINES, "markitdown", boom)
    r = extract_office(corpus / "page.html", ".html")
    assert r.status == "ok"
    assert r.engine in ("pandoc", "textutil")


def test_office_all_engines_fail_is_recorded_not_raised(corpus, monkeypatch):
    import kb.extract.office as office

    def boom(path, ext):
        raise RuntimeError("engine exploded")

    for name in office.ENGINES:
        monkeypatch.setitem(office.ENGINES, name, boom)
    r = extract_office(corpus / "page.html", ".html")
    assert r.status == "failed"
    assert "engine exploded" in (r.error or "")


# --- router error isolation --------------------------------------------------


def test_extract_file_never_raises(cfg, tmp_path, monkeypatch):
    p = tmp_path / "a.md"
    p.write_text("hello")

    def boom(path):
        raise ValueError("nope")

    monkeypatch.setattr("kb.extract.plain.extract_plain", boom)
    r = extract_file(p, ".md", "text/markdown", cfg)
    assert r.status == "failed"
    assert "ValueError" in (r.error or "")


def test_extract_file_skips_unroutable(cfg, tmp_path):
    p = tmp_path / "a.xyz"
    p.write_bytes(b"\x00\x01")
    assert extract_file(p, ".xyz", None, cfg).status == "skipped"
