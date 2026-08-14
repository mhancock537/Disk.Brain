"""Chunking: heading structure, size bounds, overlap."""

from __future__ import annotations

import random

import pytest

from kb.chunk import (
    CHARS_PER_TOKEN,
    chunk_document,
    chunk_markdown,
    estimate_tokens,
    pack,
    split_by_headings,
    strip_extraction_artifacts,
    tokens_to_chars,
    _split_oversized,
    _tail,
)


# --- token estimation --------------------------------------------------------


def test_token_estimate_round_trips():
    assert estimate_tokens("x" * 367) == 100
    assert tokens_to_chars(100) == int(100 * CHARS_PER_TOKEN)
    assert estimate_tokens("") == 1  # never zero, so a chunk always has a cost


# --- artifact stripping ------------------------------------------------------


def test_strips_extractor_header_and_page_markers():
    raw = (
        "<!-- okf-kb extraction\nsource: /a/b.pdf\nhash: abc\n-->\n\n"
        "<!-- page 1 -->\n\nReal text.\n\n<!-- page 2 -->\n\nMore text."
    )
    out = strip_extraction_artifacts(raw)
    assert "okf-kb extraction" not in out
    assert "page 1" not in out
    assert "Real text." in out and "More text." in out


# --- heading splitting -------------------------------------------------------


def test_split_by_headings_builds_a_nested_path():
    md = "# Top\n\nintro\n\n## Middle\n\nbody\n\n### Deep\n\nleaf\n"
    got = dict(split_by_headings(md))
    assert got["Top"] == "intro"
    assert got["Top > Middle"] == "body"
    assert got["Top > Middle > Deep"] == "leaf"


def test_split_by_headings_pops_back_out_of_nesting():
    md = "# A\n\na\n\n## B\n\nb\n\n# C\n\nc\n"
    paths = [p for p, _ in split_by_headings(md)]
    assert paths == ["A", "A > B", "C"]


def test_split_by_headings_keeps_preamble():
    md = "Loose opening text.\n\n# Heading\n\nbody\n"
    sections = split_by_headings(md)
    assert sections[0] == ("", "Loose opening text.")


def test_split_by_headings_handles_no_headings():
    assert split_by_headings("just text") == [("", "just text")]


def test_split_by_headings_handles_empty():
    assert split_by_headings("   ") == []


def test_headings_with_trailing_hashes():
    assert split_by_headings("## Title ##\n\nbody\n")[0][0] == "Title"


# --- packing and overlap -----------------------------------------------------


def test_pack_respects_the_limit():
    pieces = ["a" * 100 for _ in range(10)]
    for chunk in pack(pieces, max_chars=250, overlap_chars=0):
        assert len(chunk) <= 250


def test_pack_carries_overlap_between_chunks():
    pieces = [f"piece{i} " + "x" * 90 for i in range(6)]
    chunks = pack(pieces, max_chars=300, overlap_chars=120)
    assert len(chunks) > 1
    # The tail of chunk N reappears at the head of chunk N+1.
    assert chunks[0].split("\n\n")[-1][:20] in chunks[1]


def test_tail_never_exceeds_the_overlap_budget():
    huge = "y" * 5000
    tail = _tail([huge], overlap_chars=200)
    assert sum(len(t) for t in tail) <= 200


def test_tail_is_empty_when_overlap_is_zero():
    assert _tail(["abc"], 0) == []


def test_oversized_pieces_are_capped():
    for piece in _split_oversized("z" * 10_000, max_chars=500):
        assert len(piece) <= 500


def test_split_oversized_prefers_paragraph_then_sentence():
    text = "First sentence here. Second sentence here.\n\nSecond paragraph."
    pieces = _split_oversized(text, max_chars=1000)
    assert pieces == [
        "First sentence here. Second sentence here.",
        "Second paragraph.",
    ]


# --- the size guarantee ------------------------------------------------------


@pytest.mark.parametrize("max_tokens", [100, 400, 800])
def test_no_chunk_ever_exceeds_the_budget(max_tokens):
    """The bound must hold including carried overlap.

    Regression: an oversized overlap tail once let chunks reach twice the
    limit, which put 2,867 of 35,420 real corpus chunks over budget.
    """
    rng = random.Random(5)
    words = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]
    body = " ".join(rng.choice(words) for _ in range(4000))
    long_para = "q" * 9000
    md = f"# One\n\n{body}\n\n## Two\n\n{long_para}\n\n### Three\n\n{body}\n"

    for _path, chunk in chunk_markdown(md, max_tokens=max_tokens, overlap_ratio=0.15):
        assert estimate_tokens(chunk) <= max_tokens


def test_small_sections_are_left_whole():
    md = "# A\n\nshort body\n\n# B\n\nanother short body\n"
    chunks = chunk_markdown(md, max_tokens=800, overlap_ratio=0.15)
    assert [c for _p, c in chunks] == ["short body", "another short body"]


def test_chunking_is_deterministic():
    md = "# A\n\n" + ("word " * 3000)
    assert chunk_markdown(md, 200, 0.15) == chunk_markdown(md, 200, 0.15)


def test_no_empty_chunks():
    md = "# A\n\n\n\n# B\n\n   \n\n# C\n\nreal\n"
    assert all(c.strip() for _p, c in chunk_markdown(md, 800, 0.15))


# --- document level ----------------------------------------------------------


def test_chunk_document_emits_the_summary_first():
    chunks = chunk_document(
        concept_id="runbook/a",
        source_hash="abc123",
        concept_type="Runbook",
        sensitivity="work",
        title="A Runbook",
        text="# Steps\n\nDo the thing.\n",
        summary="How to do the thing.",
    )
    assert chunks[0].heading_path == "Summary"
    assert chunks[0].text == "How to do the thing."
    assert chunks[0].chunk_index == 0
    assert chunks[1].heading_path == "Steps"


def test_chunk_document_without_a_summary():
    chunks = chunk_document(
        concept_id="runbook/a", source_hash="h", concept_type="Runbook",
        sensitivity="work", title="T", text="# S\n\nbody\n", summary="  ",
    )
    assert chunks[0].heading_path == "S"


def test_chunk_ids_are_sequential_and_unique():
    chunks = chunk_document(
        concept_id="note/x", source_hash="h", concept_type="Note",
        sensitivity="work", title="T",
        text="# A\n\n" + "word " * 2000 + "\n\n# B\n\nmore\n",
        summary="s", max_tokens=200,
    )
    ids = [c.chunk_id for c in chunks]
    assert len(ids) == len(set(ids))
    assert ids == [f"note/x#{i}" for i in range(len(chunks))]


def test_chunk_carries_every_filter_field():
    c = chunk_document(
        concept_id="note/x", source_hash="hash1", concept_type="Meeting Notes",
        sensitivity="unknown", title="T", text="body", summary="",
        source="gdrive",
    )[0]
    assert c.source == "gdrive"           # Phase 8 filter
    assert c.concept_type == "Meeting Notes"
    assert c.sensitivity == "unknown"     # Phase 8 admits a third value
    assert c.source_hash == "hash1"
    assert set(c.as_row()) == {
        "chunk_id", "concept_id", "source_hash", "source", "concept_type",
        "sensitivity", "title", "heading_path", "chunk_index", "text", "tokens",
    }


def test_embed_input_prepends_title_and_heading():
    c = chunk_document(
        concept_id="c/x", source_hash="h", concept_type="Contract",
        sensitivity="work", title="MSSP Agreement",
        text="# Term and Renewal\n\nIt renews annually.\n", summary="",
    )[0]
    assert c.embed_input().startswith("MSSP Agreement > Term and Renewal")
    assert "It renews annually." in c.embed_input()


def test_embed_input_without_a_heading():
    c = chunk_document(
        concept_id="c/x", source_hash="h", concept_type="Note",
        sensitivity="work", title="Title", text="loose text", summary="",
    )[0]
    assert c.embed_input() == "Title\n\nloose text"


def test_empty_document_yields_nothing():
    assert chunk_document(
        concept_id="c/x", source_hash="h", concept_type="Note",
        sensitivity="work", title="T", text="   ", summary="",
    ) == []


# --- Chunks must carry retrievable content -----------------------------------


def test_a_bare_code_fence_never_becomes_a_chunk():
    """Found by auditing the live index: 266 chunks were fence markers.

    They cost an embedding each, occupy top-k slots, and get sent to the
    reranker, which then spends GPU deciding how relevant the string ``` is.
    """
    chunks = chunk_document(
        concept_id="a/b", source_hash="h", concept_type="Runbook",
        sensitivity="work", title="T",
        text="# Setup\n\n```bash\n\n```\n\n---\n\nRun the installer first.\n",
    )
    texts = [c.text.strip() for c in chunks]
    assert not any(t in ("```", "```bash", "---", "") for t in texts), texts
    assert any("installer" in t for t in texts), "real content must survive"


def test_a_two_character_table_cell_never_becomes_a_chunk():
    """Measured on the real corpus: a search for "quarter finals survey" put
    '30', '20' and '10' into four of its top six BM25 slots."""
    chunks = chunk_document(
        concept_id="a/b", source_hash="h", concept_type="Runbook",
        sensitivity="work", title="T", text="# Numbers\n\n30\n\n# Real\n\nActual prose here.\n",
    )
    assert all(c.text.strip() != "30" for c in chunks)


def test_short_but_real_content_is_kept():
    """The cut has to be surgical. A half-remembered command is exactly what
    this system exists to find, and several live at 12 to 40 characters."""
    for keep in ("docker stats", "networkx==3.2.1", "pytest",
                 "kubectl -n redwood describe ingress redwood"):
        chunks = chunk_document(
            concept_id="a/b", source_hash="h", concept_type="Runbook",
            sensitivity="work", title="T", text=f"# H\n\n{keep}\n",
        )
        assert any(keep in c.text for c in chunks), f"lost {keep!r}"


def test_chunk_indices_stay_contiguous_after_dropping():
    """Chunk ids are concept_id#index. Filtering after numbering would leave
    gaps, and update_index deletes by concept_id so gaps would silently persist."""
    chunks = chunk_document(
        concept_id="a/b", source_hash="h", concept_type="Runbook",
        sensitivity="work", title="T",
        text="# A\n\n```\n\n```\n\nreal one\n\n---\n\nreal two\n",
    )
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    assert [c.chunk_id for c in chunks] == [f"a/b#{i}" for i in range(len(chunks))]


def test_the_summary_chunk_survives_the_substantive_filter():
    """Caught on a second pass over checks that had already passed.

    The filter is for structural artefacts in document bodies. The summary is
    not one: it is the concept's own description, emitted as chunk 0 precisely
    so a query matching the summary but no body paragraph still finds the
    document. Dropping it silently would remove the concept's primary handle.

    No real summary is this short (shortest in corpus: 65 chars), so this
    guards the design decision rather than a live case.
    """
    chunks = chunk_document(
        concept_id="a/b", source_hash="h", concept_type="Runbook",
        sensitivity="work", title="T", text="", summary="S",
    )
    assert chunks and chunks[0].heading_path == "Summary"
    assert chunks[0].text == "S"


def test_a_body_artefact_is_still_dropped_when_a_summary_exists():
    """The exemption must not leak into body chunks."""
    chunks = chunk_document(
        concept_id="a/b", source_hash="h", concept_type="Runbook",
        sensitivity="work", title="T", text="# H\n\n```\n\n```\n", summary="A real summary.",
    )
    assert [c.heading_path for c in chunks] == ["Summary"]
