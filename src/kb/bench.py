"""Embedding model benchmark: held-out sentence probes.

There are no relevance labels for this corpus, and inventing them with an LLM
would measure the LLM. So the probe is built from the corpus itself:

  1. take N chunks of real corpus text
  2. from a sample of them, lift one distinctive sentence out
  3. index the chunks **with that sentence removed**
  4. query with the lifted sentence and see whether its own chunk comes back

Removing the sentence is what makes this a semantic test rather than a substring
test. The query shares no exact span with its target, so a hit means the model
placed the sentence near the passage it came from.

Reported as recall@1, recall@5, recall@10 and MRR, alongside wall clock and
vector width, so the accuracy and the cost are visible together.
"""

from __future__ import annotations

import random
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .chunk import chunk_markdown, strip_extraction_artifacts
from .config import Config, get_logger
from .embed import Embedder

log = get_logger("bench")

SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'\[])")
MIN_PROBE_WORDS = 9
MAX_PROBE_WORDS = 60


@dataclass
class ModelResult:
    model: str
    dimensions: int
    chunks: int
    probes: int
    embed_seconds: float
    chunks_per_second: float
    query_seconds: float
    recall_at_1: float
    recall_at_5: float
    recall_at_10: float
    mrr: float


def _distinctive_sentence(text: str) -> str | None:
    """The longest sentence in a usable size band, skipping table and list rows."""
    best: str | None = None
    # Split on line breaks first: a markdown table or list has no terminal
    # punctuation, so a sentence split alone would return the whole block.
    candidates: list[str] = []
    for line in text.splitlines():
        candidates.extend(SENTENCE_RE.split(line))
    for raw in candidates:
        s = " ".join(raw.split())
        if s.startswith(("|", "-", "*", "#", ">")) or "|" in s:
            continue
        words = s.split()
        if not (MIN_PROBE_WORDS <= len(words) <= MAX_PROBE_WORDS):
            continue
        if best is None or len(words) > len(best.split()):
            best = s
    return best


def collect_chunks(cfg: Config, target: int, seed: int = 11) -> list[str]:
    """Chunk real extracted documents until `target` chunks are gathered.

    Source text comes from `data/extracted/`, not the bundle, so the benchmark
    can run at full size before enrichment has finished. What is being measured
    is the embedding model, and that does not care where the prose came from.
    """
    conn = sqlite3.connect(cfg.manifest_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT DISTINCT hash, extract_path FROM files
        WHERE scan_status = 'included' AND extract_status = 'ok'
          AND word_count BETWEEN 200 AND 20000
        GROUP BY hash ORDER BY hash
        """
    ).fetchall()
    conn.close()

    rng = random.Random(seed)
    rng.shuffle(rows)

    out: list[str] = []
    for row in rows:
        path = Path(row["extract_path"])
        if not path.is_file():
            continue
        text = strip_extraction_artifacts(
            path.read_text(encoding="utf-8", errors="replace")
        )
        for _heading, body in chunk_markdown(
            text, cfg.chunk.max_tokens, cfg.chunk.overlap_ratio
        ):
            if len(body.split()) >= 40:
                out.append(body)
            if len(out) >= target:
                return out
    return out


def build_probes(
    chunks: list[str], count: int, seed: int = 11
) -> tuple[list[str], list[tuple[str, int]]]:
    """Return (indexed_chunks, probes) where each probe is (query, target_index).

    The indexed copy of a probed chunk has its probe sentence removed.
    """
    rng = random.Random(seed)
    order = list(range(len(chunks)))
    rng.shuffle(order)

    indexed = list(chunks)
    probes: list[tuple[str, int]] = []

    for i in order:
        if len(probes) >= count:
            break
        sentence = _distinctive_sentence(chunks[i])
        if sentence is None:
            continue
        stripped = " ".join(chunks[i].replace(sentence, " ").split())
        if len(stripped.split()) < 25:
            continue  # too little left to be a fair target
        indexed[i] = stripped
        probes.append((sentence, i))
    return indexed, probes


def evaluate(model: str, indexed: list[str], probes: list[tuple[str, int]],
             batch_size: int) -> ModelResult:
    embedder = Embedder(model=model, batch_size=batch_size)
    dim = embedder.dim

    t0 = time.monotonic()
    doc_vectors = embedder.encode_batched(indexed)
    embed_seconds = time.monotonic() - t0

    t1 = time.monotonic()
    query_vectors = embedder.encode_batched([q for q, _ in probes])
    query_seconds = time.monotonic() - t1

    import numpy as np

    # Vectors are L2-normalised, so a dot product is the cosine similarity.
    docs = np.asarray(doc_vectors, dtype=np.float32)
    queries = np.asarray(query_vectors, dtype=np.float32)
    scores = queries @ docs.T

    hits = {1: 0, 5: 0, 10: 0}
    reciprocal = 0.0
    for row, (_query, target) in zip(scores, probes):
        # Rank of the target = how many chunks scored strictly higher, plus one.
        rank = int((row > row[target]).sum()) + 1
        for k in hits:
            if rank <= k:
                hits[k] += 1
        reciprocal += 1.0 / rank

    n = len(probes)
    return ModelResult(
        model=model,
        dimensions=dim,
        chunks=len(indexed),
        probes=n,
        embed_seconds=round(embed_seconds, 1),
        chunks_per_second=round(len(indexed) / embed_seconds, 1) if embed_seconds else 0.0,
        query_seconds=round(query_seconds, 1),
        recall_at_1=round(hits[1] / n, 3) if n else 0.0,
        recall_at_5=round(hits[5] / n, 3) if n else 0.0,
        recall_at_10=round(hits[10] / n, 3) if n else 0.0,
        mrr=round(reciprocal / n, 3) if n else 0.0,
    )


def run_benchmark(
    cfg: Config, models: list[str], chunks: int = 500, probes: int = 150
) -> tuple[list[ModelResult], dict]:
    pool = collect_chunks(cfg, chunks)
    if len(pool) < 50:
        raise RuntimeError(
            f"only {len(pool)} chunks available. Run `kb extract` first."
        )
    indexed, probe_list = build_probes(pool, probes)
    log.info("benchmarking %d models over %d chunks, %d probes",
             len(models), len(indexed), len(probe_list))

    results = [evaluate(m, indexed, probe_list, cfg.embed.batch_size) for m in models]
    meta = {
        "chunks_indexed": len(indexed),
        "probes": len(probe_list),
        "method": "held-out sentence removed from its own chunk before indexing",
    }
    return results, meta
