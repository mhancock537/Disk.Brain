"""Apple Vision OCR through ocrmac.

Vision runs on-device. Nothing is uploaded. ocrmac hands back
(text, confidence, bbox) triples in reading order; we keep the text and use
the bounding boxes only to decide where lines break.
"""

from __future__ import annotations

import io
from pathlib import Path

from ..config import Config, get_logger
from . import ExtractResult

log = get_logger("extract.ocr")

# Vision returns per-token boxes as (x, y, width, height) in normalised
# coordinates with the origin bottom-left. Tokens whose vertical centres differ
# by more than this belong to different lines.
LINE_TOLERANCE = 0.012


def _lines_from_annotations(annotations) -> str:
    """Group Vision tokens into lines by vertical position."""
    rows: list[tuple[float, list[tuple[float, str]]]] = []
    for text, _conf, bbox in annotations:
        if not text.strip():
            continue
        x, y, _w, h = bbox
        centre = y + h / 2
        for row_centre, tokens in rows:
            if abs(row_centre - centre) <= LINE_TOLERANCE:
                tokens.append((x, text))
                break
        else:
            rows.append((centre, [(x, text)]))

    # Top of the page first: Vision's origin is bottom-left, so descending y.
    rows.sort(key=lambda r: -r[0])
    return "\n".join(
        " ".join(t for _x, t in sorted(tokens, key=lambda t: t[0]))
        for _centre, tokens in rows
    ).strip()


def ocr_image_bytes(data: bytes, cfg: Config) -> str:
    from PIL import Image
    from ocrmac import ocrmac

    with Image.open(io.BytesIO(data)) as img:
        img.load()
        result = ocrmac.OCR(
            img, framework="vision", recognition_level="accurate", detail=True
        ).recognize()
    return _lines_from_annotations(result)


def ocr_pdf(path: Path, cfg: Config, page_numbers: list[int] | None = None) -> ExtractResult:
    """Render pages to PNG in memory and run Vision over each."""
    import pymupdf

    parts: list[str] = []
    truncated = False
    with pymupdf.open(path) as doc:
        pages = page_numbers if page_numbers is not None else list(range(doc.page_count))
        if len(pages) > cfg.ocr.max_pages:
            pages = pages[: cfg.ocr.max_pages]
            truncated = True
        for pno in pages:
            try:
                pix = doc.load_page(pno).get_pixmap(dpi=cfg.ocr.dpi)
                text = ocr_image_bytes(pix.tobytes("png"), cfg)
            except Exception as exc:
                log.warning("ocr failed on page %d of %s (%s)", pno + 1, path.name, exc)
                continue
            if text:
                parts.append(f"<!-- page {pno + 1} -->\n\n{text}")

    body = "\n\n".join(parts).strip()
    if not body:
        return ExtractResult(status="empty", engine="ocrmac/vision")
    if truncated:
        body += f"\n\n<!-- truncated at ocr.max_pages = {cfg.ocr.max_pages} -->"
    return ExtractResult(
        status="ok",
        text=body,
        engine="ocrmac/vision",
        meta={"ocr_pages": len(parts), "truncated": truncated},
    )


def extract_image(path: Path, cfg: Config) -> ExtractResult:
    text = ocr_image_bytes(path.read_bytes(), cfg)
    if not text:
        return ExtractResult(status="empty", engine="ocrmac/vision")
    return ExtractResult(status="ok", text=text, engine="ocrmac/vision")
