"""Chunking: markdown headings first, then size-bounded splits with overlap.

Token counts are estimated from characters. The ratio was measured against the
enrichment model itself on 12 real corpus documents (48,000 characters), not
assumed: `ollama.generate(..., num_predict=1)` reports `prompt_eval_count`, and
the median came to 3.67 characters per token. Using a real tokenizer would mean
adding `transformers` and downloading a second copy of the vocabulary.

The chunker takes a strategy so that Phase 8's speaker-turn chunking for meeting
transcripts slots in beside this one rather than replacing it.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Callable, Iterable

# Measured 2026-08-07 against qwen3:14b-q4_K_M on corpus text. See module docstring.
CHARS_PER_TOKEN = 3.67

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$", re.MULTILINE)
EXTRACT_HEADER_RE = re.compile(r"\A<!--\s*okf-kb extraction.*?-->\s*", re.DOTALL)
PAGE_MARKER_RE = re.compile(r"^<!-- page \d+ -->\s*$", re.MULTILINE)
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'\[])")
WORD_CHAR_RE = re.compile(r"\w")


def estimate_tokens(text: str) -> int:
    return max(1, round(len(text) / CHARS_PER_TOKEN))


def tokens_to_chars(tokens: int) -> int:
    return int(tokens * CHARS_PER_TOKEN)


@dataclass
class Chunk:
    chunk_id: str
    concept_id: str
    source_hash: str
    source: str          # local | gdrive | onedrive | fireflies | granola
    concept_type: str
    sensitivity: str     # work | personal | unknown
    title: str
    heading_path: str
    chunk_index: int
    text: str
    tokens: int

    def embed_input(self) -> str:
        """What actually goes to the embedding model.

        The title and heading path are prepended so an isolated paragraph keeps
        the context that makes it findable. A chunk reading "It renews annually"
        is meaningless alone and unambiguous under
        "Master Services Agreement > Term and Renewal".
        """
        head = self.title
        if self.heading_path:
            head = f"{head} > {self.heading_path}"
        return f"{head}\n\n{self.text}"

    def as_row(self) -> dict:
        return asdict(self)


# --- markdown splitting ------------------------------------------------------


def strip_extraction_artifacts(text: str) -> str:
    """Remove the extractor's own header comment and per-page markers."""
    text = EXTRACT_HEADER_RE.sub("", text)
    text = PAGE_MARKER_RE.sub("", text)
    return text.strip()


CODE_FENCE_RE = re.compile(r"^\s*```[\w-]*\s*$", re.MULTILINE)
MIN_WORD_CHARS = 3


def is_substantive(text: str) -> bool:
    """Whether a chunk carries anything retrievable.

    A heading section whose whole body is a code fence, a horizontal rule, or a
    single table cell produces a chunk like '```bash', '---' or '30'. Each of
    those costs an embedding, occupies a top-k slot, and is then handed to the
    reranker, which spends GPU deciding how relevant the string '30' is. On the
    real corpus that put '30', '20' and '10' into four of the top six BM25 slots
    for an ordinary query.

    Deliberately a low bar rather than a length threshold. The 30-to-60
    character band holds `docker stats`, `networkx==3.2.1` and
    `kubectl -n redwood describe ingress redwood`, and a half-remembered
    command is exactly what this system exists to find. Measured on the live
    index: this drops 266 chunks of 35,027, all of them fences and rules.
    """
    return len(WORD_CHAR_RE.findall(CODE_FENCE_RE.sub("", text or ""))) >= MIN_WORD_CHARS


def split_by_headings(text: str) -> list[tuple[str, str]]:
    """Return (heading_path, body) pairs, preserving heading nesting.

    A document with no headings yields a single pair with an empty path.
    """
    matches = list(HEADING_RE.finditer(text))
    if not matches:
        body = text.strip()
        return [("", body)] if body else []

    sections: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []

    preamble = text[: matches[0].start()].strip()
    if preamble:
        sections.append(("", preamble))

    for i, m in enumerate(matches):
        level = len(m.group(1))
        heading = m.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, heading))
        path = " > ".join(h for _lvl, h in stack)

        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end() : end].strip()
        if body:
            sections.append((path, body))
    return sections


def _split_oversized(text: str, max_chars: int) -> list[str]:
    """Paragraph boundaries first, then sentences, then a hard cut."""
    pieces: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            pieces.append(para)
            continue
        for sentence in SENTENCE_RE.split(para):
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) <= max_chars:
                pieces.append(sentence)
            else:
                # A single sentence longer than the budget: a table row, a
                # base64 blob, a minified line. Cut it on length.
                for i in range(0, len(sentence), max_chars):
                    pieces.append(sentence[i : i + max_chars])
    return pieces


def pack(pieces: Iterable[str], max_chars: int, overlap_chars: int) -> list[str]:
    """Greedily fill chunks, carrying a tail of the previous one as overlap."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0

    for piece in pieces:
        if current and size + len(piece) + 2 > max_chars:
            chunks.append("\n\n".join(current))
            current = _tail(current, overlap_chars) if overlap_chars > 0 else []
            size = sum(len(p) + 2 for p in current)
            # The carried overlap must not push this chunk over budget. When
            # the incoming piece is large enough that it would, the overlap is
            # dropped rather than the size limit being broken.
            if size + len(piece) + 2 > max_chars:
                current, size = [], 0
        current.append(piece)
        size += len(piece) + (2 if len(current) > 1 else 0)

    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _tail(pieces: list[str], overlap_chars: int) -> list[str]:
    """The last whole pieces that fit inside the overlap budget.

    A single trailing piece larger than the budget is truncated to its final
    `overlap_chars` characters, cut at a word boundary. Returning it whole
    would let the next chunk reach twice the size limit.
    """
    if overlap_chars <= 0 or not pieces:
        return []
    out: list[str] = []
    total = 0
    for piece in reversed(pieces):
        if total + len(piece) > overlap_chars:
            if not out:
                snippet = piece[-overlap_chars:]
                cut = snippet.find(" ")
                trimmed = snippet[cut + 1 :] if cut != -1 else snippet
                if trimmed.strip():
                    out.insert(0, trimmed)
            break
        out.insert(0, piece)
        total += len(piece)
    return out


def chunk_markdown(
    text: str, max_tokens: int = 800, overlap_ratio: float = 0.15
) -> list[tuple[str, str]]:
    """Return (heading_path, chunk_text) pairs.

    Headings come first, so a chunk boundary lands on a section break wherever a
    section is small enough to stand alone. Only oversized sections get split
    further, and only those carry overlap.
    """
    max_chars = tokens_to_chars(max_tokens)
    overlap_chars = int(max_chars * overlap_ratio)

    out: list[tuple[str, str]] = []
    for path, body in split_by_headings(strip_extraction_artifacts(text)):
        if not body:
            continue
        if len(body) <= max_chars:
            out.append((path, body))
            continue
        for piece in pack(_split_oversized(body, max_chars), max_chars, overlap_chars):
            if piece.strip():
                out.append((path, piece))
    return out


# --- strategies --------------------------------------------------------------

Strategy = Callable[[str, int, float], list[tuple[str, str]]]

STRATEGIES: dict[str, Strategy] = {
    "markdown": chunk_markdown,
    # Phase 8B adds "transcript": speaker turns grouped into 400-600 token
    # windows with two turns of overlap.
}


def chunk_document(
    *,
    concept_id: str,
    source_hash: str,
    concept_type: str,
    sensitivity: str,
    title: str,
    text: str,
    summary: str = "",
    source: str = "local",
    strategy: str = "markdown",
    max_tokens: int = 800,
    overlap_ratio: float = 0.15,
) -> list[Chunk]:
    """Chunk one document into records ready for both indexes.

    The concept's own one-sentence summary is emitted as chunk 0. It is the
    best short description of the document that exists, and without it a query
    matching the summary but no body paragraph would find nothing.
    """
    split = STRATEGIES[strategy]
    chunks: list[Chunk] = []

    def add(heading_path: str, body: str, always: bool = False) -> None:
        # Filter here, where indices are assigned, so chunk_index stays
        # contiguous. Filtering afterwards would leave gaps in chunk ids, and
        # update_index deletes by concept_id, so the gaps would persist.
        #
        # `always` exempts the summary. The filter exists to drop structural
        # artefacts out of document bodies, and the summary is not one: it is
        # the concept's own description, deliberately emitted as chunk 0
        # because a query matching the summary but no body paragraph would
        # otherwise find nothing. If enrichment ever produces a two-character
        # summary that is an enrichment problem, and it should surface rather
        # than be swallowed here. No such summary exists today: the shortest in
        # the corpus is 65 characters and the 5th percentile is 96.
        if not always and not is_substantive(body):
            return
        index = len(chunks)
        chunks.append(
            Chunk(
                chunk_id=f"{concept_id}#{index}",
                concept_id=concept_id,
                source_hash=source_hash,
                source=source,
                concept_type=concept_type,
                sensitivity=sensitivity,
                title=title,
                heading_path=heading_path,
                chunk_index=index,
                text=body,
                tokens=estimate_tokens(body),
            )
        )

    if summary.strip():
        add("Summary", summary.strip(), always=True)
    for heading_path, body in split(text, max_tokens, overlap_ratio):
        add(heading_path, body)
    return chunks
