"""Hybrid retrieval: vector, BM25, reciprocal rank fusion, graph, reranker.

The pipeline, in order:

  1. embed the query and take the top 50 by cosine from LanceDB
  2. take the top 50 by BM25 from FTS5
  3. fuse both lists with reciprocal rank fusion at k=60
  4. map the top 20 fused chunks to their concepts
  5. expand one hop along LINKS_TO and pull chunks from the neighbours
  6. rerank every candidate with Qwen3-Reranker
  7. return the top 15 with concept frontmatter and source file path attached

The reranker runs under MLX, not Ollama. Ollama exposes no rerank endpoint, its
`logprobs` come back null, and the only community Qwen3-Reranker GGUF emits
"!!!!" instead of a judgement. Both were tested on 2026-08-07 before switching.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import Config, get_logger
from .embed import Embedder, open_table
from .okf import parse

log = get_logger("retrieve")

# FTS5 treats these as syntax. A user query is data, so they are stripped
# rather than escaped: a stray quote must not become a malformed MATCH.
FTS_STRIP_RE = re.compile(r'[^\w\s]', re.UNICODE)


@dataclass
class Hit:
    chunk_id: str
    concept_id: str
    title: str
    heading_path: str
    text: str
    source: str
    concept_type: str
    sensitivity: str
    vector_rank: int | None = None
    bm25_rank: int | None = None
    from_graph: bool = False
    rrf: float = 0.0
    rerank: float = 0.0
    concept: dict[str, Any] = field(default_factory=dict)
    file_path: str = ""

    @property
    def score(self) -> float:
        """Reranker score when it ran, fused rank score otherwise."""
        return self.rerank if self.rerank else self.rrf

    def snippet(self, width: int = 240) -> str:
        body = " ".join(self.text.split())
        return body[:width] + ("..." if len(body) > width else "")


def sanitize_fts(query: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression.

    Terms are quoted and OR'd, so partial matches still score rather than the
    whole query failing when one word is absent.
    """
    terms = [t for t in FTS_STRIP_RE.sub(" ", query).split() if len(t) > 1]
    if not terms:
        return ""
    return " OR ".join(f'"{t}"' for t in terms)


def _filter_clause(
    sensitivity: str | None, source: str | None, concept_type: str | None
) -> tuple[str, list[str]]:
    """SQL fragment shared by the vector prefilter and the BM25 query."""
    parts: list[str] = []
    values: list[str] = []
    for column, value in (
        ("sensitivity", sensitivity),
        ("source", source),
        ("concept_type", concept_type),
    ):
        if value:
            parts.append(f"{column} = ?")
            values.append(value)
    return (" AND ".join(parts), values)


# --- the three retrievers ----------------------------------------------------


def vector_search(
    cfg: Config, query_vector: Sequence[float], top_k: int,
    sensitivity: str | None, source: str | None, concept_type: str | None,
) -> list[dict]:
    table = open_table(cfg)
    builder = table.search(list(query_vector), vector_column_name="vector").metric(
        cfg.embed.metric
    )
    clause, values = _filter_clause(sensitivity, source, concept_type)
    if clause:
        # LanceDB takes a literal SQL predicate, so the values are inlined
        # here. They come from a closed set of enum-like filters, never from
        # free text, and each is quote-escaped.
        literal = clause
        for value in values:
            literal = literal.replace("?", "'" + value.replace("'", "''") + "'", 1)
        builder = builder.where(literal, prefilter=True)
    return builder.limit(top_k).to_list()


def bm25_search(
    cfg: Config, query: str, top_k: int,
    sensitivity: str | None, source: str | None, concept_type: str | None,
) -> list[dict]:
    match = sanitize_fts(query)
    if not match or not cfg.fts_path.is_file():
        return []

    clause, values = _filter_clause(sensitivity, source, concept_type)
    where = f" AND {clause}" if clause else ""
    conn = sqlite3.connect(cfg.fts_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"""
            SELECT chunk_id, concept_id, source, concept_type, sensitivity,
                   title, heading_path, text, bm25(chunks) AS score
            FROM chunks WHERE chunks MATCH ?{where}
            ORDER BY bm25(chunks) LIMIT ?
            """,
            [match, *values, top_k],
        ).fetchall()
    except sqlite3.OperationalError as exc:
        log.warning("BM25 query failed for %r (%s)", query, exc)
        return []
    finally:
        conn.close()
    return [dict(r) for r in rows]


def graph_neighbours(cfg: Config, concept_ids: Iterable[str], hops: int) -> set[str]:
    """Concepts one hop away along LINKS_TO, in either direction.

    LINKS_TO is stored directed, but relatedness in this bundle is symmetric:
    A citing B is as good a signal for B as for A.
    """
    ids = [c for c in concept_ids if c]
    if not ids or hops < 1:
        return set()
    graph_path = cfg.graph_dir
    if not (graph_path.exists() or graph_path.is_dir()):
        return set()

    from .graph import query as graph_query

    listed = ", ".join("'" + c.replace("'", "''") + "'" for c in ids)
    try:
        df = graph_query(
            cfg,
            f"""
            MATCH (a:Concept)-[:LINKS_TO]-(b:Concept)
            WHERE a.concept_id IN [{listed}]
            RETURN DISTINCT b.concept_id AS concept_id
            """,
        )
    except Exception as exc:
        log.warning("graph expansion skipped (%s)", exc)
        return set()
    return {str(v) for v in df["concept_id"].tolist()} - set(ids)


def chunks_for_concepts(
    cfg: Config, concept_ids: Iterable[str], per_concept: int
) -> list[dict]:
    """The first chunks of each named concept, summary first."""
    ids = list(concept_ids)
    if not ids or not cfg.fts_path.is_file():
        return []
    conn = sqlite3.connect(cfg.fts_path)
    conn.row_factory = sqlite3.Row
    out: list[dict] = []
    try:
        for cid in ids:
            rows = conn.execute(
                "SELECT chunk_id, concept_id, source, concept_type, sensitivity, "
                "title, heading_path, text FROM chunks WHERE concept_id = ? "
                "LIMIT ?",
                (cid, per_concept),
            ).fetchall()
            out.extend(dict(r) for r in rows)
    finally:
        conn.close()
    return out


# --- fusion ------------------------------------------------------------------


def reciprocal_rank_fusion(
    ranked_lists: Sequence[Sequence[str]], k: int = 60
) -> dict[str, float]:
    """Standard RRF: each list contributes 1/(k + rank), ranks starting at 1.

    k damps the top of any single list, so one retriever's confident but wrong
    first result cannot dominate the fused ordering.
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for position, key in enumerate(ranked, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + position)
    return scores


# --- reranking ---------------------------------------------------------------


@lru_cache(maxsize=2)
def _load_reranker(repo: str):
    from mlx_lm import load

    log.info("loading reranker %s", repo)
    model, tokenizer = load(repo)
    yes_id = tokenizer.encode("yes", add_special_tokens=False)[0]
    no_id = tokenizer.encode("no", add_special_tokens=False)[0]
    return model, tokenizer, yes_id, no_id


def rerank(cfg: Config, query: str, hits: list[Hit]) -> None:
    """Score every hit in place with Qwen3-Reranker.

    The model answers a yes/no question, and the score is the softmax of the
    two logits at the final position. That is the documented way to use this
    model, and it needs one forward pass rather than any generation.
    """
    if not hits or not cfg.retrieve.rerank_enabled:
        return
    import mlx.core as mx

    model, tokenizer, yes_id, no_id = _load_reranker(cfg.retrieve.rerank_model)
    instruction = cfg.retrieve.rerank_instruction
    prefix = (
        "<|im_start|>system\n"
        f"{instruction} Note that the answer can only be \"yes\" or \"no\"."
        "<|im_end|>\n<|im_start|>user\n"
    )
    suffix = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"

    encoded: list[tuple[Hit, list[int]]] = []
    for hit in hits:
        heading = f"{hit.title} > {hit.heading_path}" if hit.heading_path else hit.title
        document = f"{heading}\n{hit.snippet(cfg.retrieve.rerank_doc_chars)}"
        text = (
            f"{prefix}<Instruct>: {instruction}\n<Query>: {query}\n"
            f"<Document>: {document}{suffix}"
        )
        encoded.append((hit, tokenizer.encode(text, add_special_tokens=False)))

    # Batch the forward passes. One prompt at a time cost 120 ms each, which
    # was 7.2 s of an 8.9 s query. Candidates are sorted by length first so a
    # batch pads to nearly its own longest member instead of the global one.
    encoded.sort(key=lambda pair: len(pair[1]))
    pad_id = getattr(tokenizer, "pad_token_id", None)
    if pad_id is None:
        pad_id = getattr(tokenizer, "eos_token_id", 0) or 0

    batch_size = max(1, cfg.retrieve.rerank_batch_size)
    for start in range(0, len(encoded), batch_size):
        group = encoded[start : start + batch_size]
        width = max(len(ids) for _hit, ids in group)
        try:
            # Right padding is safe for a causal model: pad tokens sit after
            # the real ones, so they cannot influence any earlier position.
            padded = mx.array([ids + [pad_id] * (width - len(ids)) for _h, ids in group])
            logits = model(padded)
            for row, (hit, ids) in enumerate(group):
                last = logits[row, len(ids) - 1]
                pair = mx.stack([last[no_id], last[yes_id]])
                hit.rerank = float(mx.softmax(pair.astype(mx.float32))[1])
        except Exception as exc:  # one bad batch never sinks a query
            log.warning("rerank batch failed (%s), falling back to zero", exc)
            for hit, _ids in group:
                hit.rerank = 0.0


# --- concept metadata --------------------------------------------------------


def load_concept_frontmatter(cfg: Config, concept_ids: Iterable[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for cid in set(concept_ids):
        path = cfg.bundle_dir / f"{cid}.md"
        if not path.is_file():
            continue
        doc = parse(path.read_text(encoding="utf-8", errors="replace"))
        if doc is not None:
            out[cid] = doc.frontmatter
    return out


def _file_path_from(frontmatter: dict) -> str:
    """The source document's real path, decoded back from its file:// URI."""
    resource = str(frontmatter.get("resource") or "")
    if resource.startswith("file://"):
        from urllib.parse import unquote, urlparse

        return unquote(urlparse(resource).path)
    for entry in frontmatter.get("sources") or []:
        if isinstance(entry, dict) and str(entry.get("resource", "")).startswith("file://"):
            from urllib.parse import unquote, urlparse

            return unquote(urlparse(str(entry["resource"])).path)
    return ""


# --- the pipeline ------------------------------------------------------------


def search(
    cfg: Config,
    query: str,
    limit: int | None = None,
    sensitivity_filter: str | None = None,
    source_filter: str | None = None,
    concept_type_filter: str | None = None,
    on_embed_error: str = "raise",
) -> tuple[list[Hit], dict]:
    """Run the whole hybrid pipeline. Returns (hits, timing and stage counts).

    `on_embed_error` controls what happens when the embedder (Ollama) is down.
    "raise", the default, lets the exception propagate: the CLI, the MCP
    server and the eval harness all depend on a failed embed stopping the
    query rather than silently returning keyword-only results. "degrade"
    keeps the query alive without Ollama, running BM25 only; anything else
    is treated the same as "raise".
    """
    rc = cfg.retrieve
    limit = limit or rc.limit
    timings: dict[str, Any] = {}
    t_start = time.monotonic()

    # 1. vector
    # The embed step is the only one that depends on Ollama being up. A dead
    # Ollama should not sink the whole query for a tool whose entire purpose
    # is finding a half-remembered document: keyword-only results beat an
    # error page. But that trade only makes sense for the web UI, so the
    # default preserves today's behaviour (raise) for every other caller.
    t0 = time.monotonic()
    degraded = ""
    query_vector: list[float] | None
    try:
        embedder = Embedder(model=cfg.embed.model, batch_size=1)
        query_vector = embedder.encode([query])[0]
    except Exception as exc:
        if on_embed_error != "degrade":
            raise
        degraded = "semantic search unavailable"
        log.warning("embed failed, degrading to keyword-only search (%s)", exc)
        query_vector = None
    timings["embed_ms"] = round((time.monotonic() - t0) * 1000, 1)
    timings["degraded"] = degraded

    t0 = time.monotonic()
    vector_rows = (
        vector_search(
            cfg, query_vector, rc.vector_top_k,
            sensitivity_filter, source_filter, concept_type_filter,
        )
        if query_vector is not None
        else []
    )
    timings["vector_ms"] = round((time.monotonic() - t0) * 1000, 1)

    # 2. BM25
    t0 = time.monotonic()
    bm25_rows = bm25_search(
        cfg, query, rc.bm25_top_k,
        sensitivity_filter, source_filter, concept_type_filter,
    )
    timings["bm25_ms"] = round((time.monotonic() - t0) * 1000, 1)

    by_id: dict[str, Hit] = {}

    def upsert(row: dict, *, from_graph: bool = False) -> Hit:
        hit = by_id.get(row["chunk_id"])
        if hit is None:
            hit = Hit(
                chunk_id=row["chunk_id"],
                concept_id=row["concept_id"],
                title=row.get("title", ""),
                heading_path=row.get("heading_path", ""),
                text=row.get("text", ""),
                source=row.get("source", "local"),
                concept_type=row.get("concept_type", ""),
                sensitivity=row.get("sensitivity", ""),
                from_graph=from_graph,
            )
            by_id[hit.chunk_id] = hit
        return hit

    for rank, row in enumerate(vector_rows, start=1):
        upsert(row).vector_rank = rank
    for rank, row in enumerate(bm25_rows, start=1):
        upsert(row).bm25_rank = rank

    # 3. reciprocal rank fusion
    fused = reciprocal_rank_fusion(
        [
            [r["chunk_id"] for r in vector_rows],
            [r["chunk_id"] for r in bm25_rows],
        ],
        k=rc.rrf_k,
    )
    for chunk_id, score in fused.items():
        if chunk_id in by_id:
            by_id[chunk_id].rrf = score

    # 4. top fused chunks -> concepts
    ordered = sorted(by_id.values(), key=lambda h: -h.rrf)
    seed_concepts = {h.concept_id for h in ordered[: rc.fuse_top_n]}
    timings["fused"] = len(by_id)
    timings["seed_concepts"] = len(seed_concepts)

    # 5. one hop in the graph
    t0 = time.monotonic()
    neighbours = graph_neighbours(cfg, seed_concepts, rc.graph_hops)
    for row in chunks_for_concepts(cfg, neighbours, rc.graph_neighbour_chunks):
        if row["chunk_id"] not in by_id:
            upsert(row, from_graph=True)
    timings["graph_ms"] = round((time.monotonic() - t0) * 1000, 1)
    timings["graph_neighbours"] = len(neighbours)

    # 6. rerank the capped candidate set
    candidates = sorted(
        by_id.values(), key=lambda h: (-h.rrf, h.from_graph)
    )[: rc.rerank_max_candidates]
    t0 = time.monotonic()
    rerank(cfg, query, candidates)
    timings["rerank_ms"] = round((time.monotonic() - t0) * 1000, 1)
    timings["reranked"] = len(candidates)

    # 7. top N, with concept frontmatter and source path attached
    top = sorted(candidates, key=lambda h: (-h.score, h.chunk_id))[:limit]
    frontmatter = load_concept_frontmatter(cfg, (h.concept_id for h in top))
    for hit in top:
        hit.concept = frontmatter.get(hit.concept_id, {})
        hit.file_path = _file_path_from(hit.concept)

    timings["total_ms"] = round((time.monotonic() - t_start) * 1000, 1)
    timings["returned"] = len(top)
    return top, timings
