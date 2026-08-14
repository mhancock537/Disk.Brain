"""Office, HTML and EPUB documents.

Three engines, tried in an order chosen per extension:

  markitdown  best markdown structure for OOXML and HTML
  pandoc      handles EPUB, ODF and RTF, which markitdown does not
  textutil    macOS built-in, the only thing here that reads legacy .doc

Falling through the chain is normal, not an error. Only an all-engines failure
is recorded as a failure.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from ..config import get_logger
from . import ExtractResult

log = get_logger("extract.office")

TIMEOUT = 120  # seconds per engine, per file

# pandoc's own name for each input format.
PANDOC_FORMAT = {
    ".docx": "docx",
    ".odt": "odt",
    ".epub": "epub",
    ".html": "html",
    ".htm": "html",
    ".xhtml": "html",
    ".rtf": "rtf",
}

# Engine order per extension. First success wins.
CHAIN: dict[str, tuple[str, ...]] = {
    ".docx": ("markitdown", "pandoc", "textutil"),
    ".doc": ("textutil", "markitdown"),
    ".xlsx": ("markitdown",),
    ".xlsm": ("markitdown",),
    ".xls": ("markitdown",),
    ".pptx": ("markitdown",),
    ".ppt": ("markitdown",),
    ".msg": ("markitdown",),
    ".html": ("markitdown", "pandoc", "textutil"),
    ".htm": ("markitdown", "pandoc", "textutil"),
    ".xhtml": ("markitdown", "pandoc"),
    ".epub": ("pandoc", "markitdown"),
    ".odt": ("pandoc", "markitdown"),
    ".ods": ("pandoc",),
    ".odp": ("pandoc",),
    ".rtf": ("textutil", "pandoc"),
    ".pages": ("textutil",),
    ".numbers": ("textutil",),
    ".key": ("textutil",),
}


def _markitdown(path: Path, ext: str) -> str:
    from markitdown import MarkItDown

    md = MarkItDown(enable_plugins=False)
    return (md.convert(str(path)).text_content or "").strip()


def _pandoc(path: Path, ext: str) -> str:
    fmt = PANDOC_FORMAT.get(ext)
    cmd = ["pandoc", "--to", "markdown-raw_html", "--wrap", "none"]
    if fmt:
        cmd += ["--from", fmt]
    cmd.append(str(path))
    proc = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"pandoc exit {proc.returncode}: {proc.stderr.decode(errors='replace')[:300]}"
        )
    return proc.stdout.decode("utf-8", errors="replace").strip()


def _textutil(path: Path, ext: str) -> str:
    """macOS textutil writes to a file, so route it through a temp path."""
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
        out = Path(tmp.name)
    try:
        proc = subprocess.run(
            ["textutil", "-convert", "txt", "-encoding", "UTF-8",
             "-output", str(out), str(path)],
            capture_output=True,
            timeout=TIMEOUT,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"textutil exit {proc.returncode}: "
                f"{proc.stderr.decode(errors='replace')[:300]}"
            )
        return out.read_text(encoding="utf-8", errors="replace").strip()
    finally:
        out.unlink(missing_ok=True)


ENGINES = {"markitdown": _markitdown, "pandoc": _pandoc, "textutil": _textutil}


def extract_office(path: Path, ext: str) -> ExtractResult:
    chain = CHAIN.get(ext, ("markitdown", "pandoc"))
    errors: list[str] = []

    for name in chain:
        try:
            text = ENGINES[name](path, ext)
        except subprocess.TimeoutExpired:
            errors.append(f"{name}: timeout after {TIMEOUT}s")
            continue
        except FileNotFoundError:
            errors.append(f"{name}: binary not installed")
            continue
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            continue

        if text:
            return ExtractResult(
                status="ok",
                text=text,
                engine=name,
                meta={"fallbacks_tried": len(errors)},
            )
        errors.append(f"{name}: produced no text")

    return ExtractResult(status="failed", engine=chain[0], error=" | ".join(errors)[:600])
