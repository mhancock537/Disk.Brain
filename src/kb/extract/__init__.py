"""Extraction router: extension and MIME in, plain markdown out.

Every extractor returns an ExtractResult rather than raising. One bad file
degrades to a recorded failure; it never stops a run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config, get_logger

log = get_logger("extract")

# Prose and tabular text: always in scope.
DOC_TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".mdx", ".rst", ".org", ".text",
    ".csv", ".tsv", ".tex", ".bib", ".vtt", ".srt",
}

# Source, config and machine formats. In scope only when
# [scan] include_source_code = true, because they inflate the corpus by 3-5x
# and answer a different kind of question than documents do.
CODE_TEXT_EXTS = {
    ".json", ".jsonl", ".ndjson", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".conf", ".env", ".properties", ".xml", ".svg",
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".astro", ".vue",
    ".sh", ".bash", ".zsh", ".sql", ".go", ".rs", ".java", ".c", ".h", ".cpp",
    ".hpp", ".cs", ".rb", ".php", ".swift", ".kt", ".lua", ".r", ".pl", ".m",
    ".scala", ".dart", ".gradle", ".css", ".scss", ".dockerfile", ".makefile",
}

PLAIN_EXTS = DOC_TEXT_EXTS | CODE_TEXT_EXTS

PDF_EXTS = {".pdf"}

OFFICE_EXTS = {
    ".docx", ".doc", ".xlsx", ".xls", ".xlsm", ".pptx", ".ppt",
    ".odt", ".ods", ".odp", ".rtf", ".msg", ".pages", ".numbers", ".key",
    ".html", ".htm", ".xhtml", ".epub",
}

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".heic", ".bmp", ".webp"}

# Extensionless files that sniffed as text still deserve a plain read.
TEXT_MIMES = {"text/plain", "text/markdown", "text/csv", "application/json"}


@dataclass
class ExtractResult:
    status: str  # "ok" | "empty" | "failed" | "skipped"
    text: str = ""
    engine: str = ""
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def word_count(self) -> int:
        return len(self.text.split())


def route(ext: str, mime: str | None, cfg: Config) -> str:
    """Name of the extractor family for this file, or "skip" if out of scope."""
    if ext in PDF_EXTS or mime == "application/pdf":
        return "pdf"
    if ext in OFFICE_EXTS:
        return "office"
    if ext in DOC_TEXT_EXTS:
        return "plain"
    if ext in CODE_TEXT_EXTS:
        return "plain" if cfg.include_source_code else "skip"
    if ext in IMAGE_EXTS:
        return "image" if (cfg.ocr.enabled and cfg.ocr.images) else "skip"
    if not ext and (mime in TEXT_MIMES or (mime or "").startswith("text/")):
        return "plain" if cfg.include_source_code else "skip"
    return "skip"


def extract_file(path: Path, ext: str, mime: str | None, cfg: Config) -> ExtractResult:
    family = route(ext, mime, cfg)
    try:
        if family == "pdf":
            from .pdf import extract_pdf

            return extract_pdf(path, cfg)
        if family == "office":
            from .office import extract_office

            return extract_office(path, ext)
        if family == "plain":
            from .plain import extract_plain

            return extract_plain(path)
        if family == "image":
            from .ocr import extract_image

            return extract_image(path, cfg)
        return ExtractResult(status="skipped", engine="none", error=f"no route for {ext or mime}")
    except Exception as exc:  # one bad file never stops a run
        log.warning("extract failed: %s (%s: %s)", path, type(exc).__name__, exc)
        return ExtractResult(
            status="failed", engine=family, error=f"{type(exc).__name__}: {exc}"
        )


def extract_out_path(cfg: Config, file_hash: str) -> Path:
    """Sharded by the first two hex characters to keep directories small."""
    return cfg.extract_out_dir / file_hash[:2] / f"{file_hash}.md"
