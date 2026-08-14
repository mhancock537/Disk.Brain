"""PDF text via PyMuPDF, with per-page scanned detection routed to OCR.

The scanned test is per page, not per document, because mixed PDFs are common:
a born-digital cover letter stapled to a scanned contract. Pages that yield
text are kept as text; only the thin pages are rendered and OCR'd.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Config, get_logger
from . import ExtractResult

log = get_logger("extract.pdf")


def _sample_indices(page_count: int, sample: int) -> list[int]:
    """Evenly spaced page indices, so a long document is judged from the whole."""
    if page_count <= sample:
        return list(range(page_count))
    step = page_count / sample
    return sorted({int(i * step) for i in range(sample)})


def extract_pdf(path: Path, cfg: Config) -> ExtractResult:
    import pymupdf

    try:
        doc = pymupdf.open(path)
    except Exception as exc:
        return ExtractResult(status="failed", engine="pymupdf", error=f"open: {exc}")

    with doc:
        if doc.needs_pass:
            return ExtractResult(
                status="skipped", engine="pymupdf", error="password protected"
            )
        page_count = doc.page_count
        if page_count == 0:
            return ExtractResult(status="empty", engine="pymupdf")

        pages: list[str] = []
        thin: list[int] = []
        for pno in range(page_count):
            try:
                text = doc.load_page(pno).get_text("text").strip()
            except Exception as exc:
                log.warning("page %d unreadable in %s (%s)", pno + 1, path.name, exc)
                text = ""
            pages.append(text)
            if len(text) < cfg.ocr.chars_per_page_threshold:
                thin.append(pno)

        sampled = _sample_indices(page_count, cfg.ocr.sample_pages)
        thin_in_sample = sum(1 for i in sampled if i in thin)
        scanned_ratio = thin_in_sample / len(sampled) if sampled else 0.0
        looks_scanned = scanned_ratio >= cfg.ocr.scanned_page_fraction

    meta = {
        "pages": page_count,
        "thin_pages": len(thin),
        "scanned_ratio": round(scanned_ratio, 2),
    }

    # Whole document reads as a scan: OCR it end to end.
    if looks_scanned and cfg.ocr.enabled:
        log.info("routing to OCR (%d pages, %.0f%% thin): %s",
                 page_count, scanned_ratio * 100, path.name)
        from .ocr import ocr_pdf

        result = ocr_pdf(path, cfg)
        result.meta.update(meta)
        result.meta["route"] = "ocr-full"
        return result

    body = "\n\n".join(
        f"<!-- page {i + 1} -->\n\n{t}" for i, t in enumerate(pages) if t
    ).strip()

    # Mostly text with a few scanned inserts: OCR just those pages.
    if thin and cfg.ocr.enabled and body:
        from .ocr import ocr_pdf

        patched = ocr_pdf(path, cfg, page_numbers=thin)
        if patched.status == "ok" and patched.text:
            body += "\n\n" + patched.text
            meta["route"] = "hybrid"
            meta["ocr_pages"] = patched.meta.get("ocr_pages", 0)
        else:
            meta["route"] = "text"
    else:
        meta["route"] = "text"

    if not body:
        if not cfg.ocr.enabled:
            return ExtractResult(
                status="empty", engine="pymupdf", error="no text layer, OCR disabled",
                meta=meta,
            )
        return ExtractResult(status="empty", engine="pymupdf", meta=meta)

    return ExtractResult(status="ok", text=body, engine="pymupdf", meta=meta)
