"""Plain text and source files. Read, decode, normalise line endings."""

from __future__ import annotations

from pathlib import Path

from . import ExtractResult

ENCODINGS = ("utf-8", "utf-16", "latin-1")

# Control bytes that never appear in real text. Whitespace and ESC are text;
# bytes at 0x80 and above are left alone, since UTF-8 and latin-1 both use them.
_CONTROL = {b for b in range(0x20) if b not in (0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x1B)}
_CONTROL.add(0x7F)
_BINARY_SNIFF = 8192
_BINARY_RATIO = 0.05


def looks_binary(raw: bytes) -> bool:
    """A NUL byte, or more than 5% control characters in the first 8 KB.

    Needed because latin-1 decodes every possible byte, so the encoding ladder
    below always "succeeds" and would happily turn an executable into mojibake.
    The thresholds match what file(1) and git use to make the same call.
    """
    sample = raw[:_BINARY_SNIFF]
    if b"\x00" in sample:
        return True
    if not sample:
        return False
    odd = sum(1 for b in sample if b in _CONTROL)
    return odd / len(sample) > _BINARY_RATIO


def extract_plain(path: Path) -> ExtractResult:
    raw = path.read_bytes()
    if not raw.strip():
        return ExtractResult(status="empty", engine="plain")

    # UTF-16 text is full of NUL bytes, so let its BOM through the sniff.
    has_utf16_bom = raw[:2] in (b"\xff\xfe", b"\xfe\xff")
    if not has_utf16_bom and looks_binary(raw):
        return ExtractResult(
            status="failed", engine="plain", error="binary content, not decodable as text"
        )

    text = None
    used = ""
    for enc in ENCODINGS:
        try:
            text = raw.decode(enc)
            used = enc
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
        used = "utf-8/replace"

    # A high replacement-character ratio means this was not really text.
    if text.count("�") > max(20, len(text) * 0.02):
        return ExtractResult(
            status="failed", engine="plain", error="binary content, not decodable as text"
        )

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return ExtractResult(status="ok", text=text, engine="plain", meta={"encoding": used})
