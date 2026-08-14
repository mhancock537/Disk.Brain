"""Retrieval evaluation: hit rate at k, MRR, and query latency.

The eval set is data, not code: `eval/questions.toml` holds each question with
the concept or concepts that genuinely answer it. Scoring is by concept, not by
chunk, because "did it find the right document" is the question that matters.

An ablation mode runs the same questions with individual stages switched off,
which is the only way to tell whether fusion, the graph hop and the reranker
are each earning their latency.
"""

from __future__ import annotations

import statistics
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config, get_logger
from .retrieve import search

log = get_logger("eval")

DEFAULT_EVAL = "eval/questions.toml"


@dataclass
class QuestionResult:
    question: str
    expected: list[str]
    rank: int | None          # 1-based rank of the first correct concept
    latency_ms: float
    top_concepts: list[str] = field(default_factory=list)
    best_score: float = 0.0   # top relevance, for abstention scoring

    @property
    def answerable(self) -> bool:
        """False for a question the corpus deliberately cannot answer."""
        return bool(self.expected)

    def hit_at(self, k: int) -> bool:
        return self.rank is not None and self.rank <= k

    def abstained(self, threshold: float) -> bool:
        """Did retrieval admit it found nothing worth returning."""
        return self.best_score < threshold


def load_questions(cfg: Config, path: Path | None = None) -> list[dict]:
    target = path or (cfg.root_dir / DEFAULT_EVAL)
    if not target.is_file():
        raise FileNotFoundError(f"no eval set at {target}")
    with target.open("rb") as fh:
        data = tomllib.load(fh)
    questions = data.get("question", [])
    if not questions:
        raise ValueError(f"{target} has no [[question]] entries")
    return questions


def run_eval(
    cfg: Config,
    path: Path | None = None,
    limit: int = 15,
    rerank: bool = True,
    graph: bool = True,
    show_progress: bool = True,
) -> dict[str, Any]:
    """Run every question and report hit rate, MRR and latency."""
    from dataclasses import replace

    questions = load_questions(cfg, path)

    run_cfg = cfg
    if not rerank:
        run_cfg = replace(run_cfg, retrieve=replace(run_cfg.retrieve, rerank_enabled=False))
    if not graph:
        run_cfg = replace(run_cfg, retrieve=replace(run_cfg.retrieve, graph_hops=0))

    results: list[QuestionResult] = []
    for i, item in enumerate(questions, start=1):
        query = item["q"]
        expected = list(item.get("concepts", []))
        t0 = time.monotonic()
        hits, _timings = search(run_cfg, query, limit=limit)
        latency = (time.monotonic() - t0) * 1000

        seen: list[str] = []
        for hit in hits:
            if hit.concept_id not in seen:
                seen.append(hit.concept_id)

        rank = None
        for position, concept_id in enumerate(seen, start=1):
            if concept_id in expected:
                rank = position
                break

        # A question with no expected concepts is one the corpus cannot
        # answer. There is no rank to find: the thing being scored is whether
        # retrieval admits it found nothing.
        best = max((h.score for h in hits), default=0.0)
        results.append(
            QuestionResult(
                query, expected, rank, round(latency, 1), seen[:10],
                best_score=round(float(best), 4),
            )
        )
        if show_progress:
            mark = f"#{rank}" if rank else "miss"
            log.info("[%d/%d] %-5s %s", i, len(questions), mark, query[:60])

    latencies = [r.latency_ms for r in results]
    n = len(results)

    # Hit rates describe answerable questions only. An unanswerable one is a
    # permanent miss by construction, so counting it would penalise the system
    # for being right.
    answerable = [r for r in results if r.answerable]
    unanswerable = [r for r in results if not r.answerable]
    a = len(answerable) or 1

    # The same threshold the interface uses to call a result set weak, read
    # from config so the two can never disagree about what "found nothing"
    # means. Taken from the caller's cfg, not run_cfg, so an ablation with the
    # reranker off is still judged against the real bar rather than a softer
    # one that would flatter it.
    threshold = cfg.retrieve.weak_score

    return {
        "questions": n,
        "answerable": len(answerable),
        "unanswerable": len(unanswerable),
        "rerank": rerank,
        "graph": graph,
        "hit_at_1": round(sum(r.hit_at(1) for r in answerable) / a, 3),
        "hit_at_3": round(sum(r.hit_at(3) for r in answerable) / a, 3),
        "hit_at_5": round(sum(r.hit_at(5) for r in answerable) / a, 3),
        "hit_at_10": round(sum(r.hit_at(10) for r in answerable) / a, 3),
        "mrr": round(
            sum(1.0 / r.rank for r in answerable if r.rank) / a, 3
        ),
        # How often the system correctly says "I found nothing" when the corpus
        # genuinely holds no answer. None when there is nothing to abstain on,
        # because a score over an empty set reads as a real number and is not.
        "abstention": (
            round(sum(r.abstained(threshold) for r in unanswerable) / len(unanswerable), 3)
            if unanswerable else None
        ),
        # The other direction, and the one that matters more. Abstention alone
        # is trivially perfect for a system that abstains on everything, which
        # is exactly what happens without the reranker: fused rank scores sit
        # near 0.016 for every query, answerable or not. This asks whether the
        # system stands behind an answer it actually found.
        "confidence": round(
            sum(not r.abstained(threshold) for r in answerable if r.rank) / a, 3
        ),
        "false_confidence": [
            {"q": r.question, "best_score": r.best_score}
            for r in unanswerable if not r.abstained(threshold)
        ],
        "false_doubt": [
            {"q": r.question, "best_score": r.best_score, "rank": r.rank}
            for r in answerable if r.rank and r.abstained(threshold)
        ],
        "misses": [r.question for r in answerable if r.rank is None],
        "latency_ms": {
            "median": round(statistics.median(latencies), 1),
            "mean": round(statistics.mean(latencies), 1),
            "min": round(min(latencies), 1),
            "max": round(max(latencies), 1),
            "p90": round(sorted(latencies)[int(n * 0.9) - 1], 1) if n >= 2 else latencies[0],
        },
        "results": results,
    }


def run_ablation(cfg: Config, path: Path | None = None, limit: int = 15) -> list[dict]:
    """Same questions, stages switched off one at a time.

    Without this the reranker's cost is unjustifiable: it is most of the query
    latency, so its contribution has to be visible rather than assumed.
    """
    configurations = [
        ("full pipeline", True, True),
        ("no reranker", False, True),
        ("no graph hop", True, False),
        ("fusion only", False, False),
    ]
    out = []
    for label, rerank, graph in configurations:
        log.info("ablation: %s", label)
        stats = run_eval(
            cfg, path, limit=limit, rerank=rerank, graph=graph, show_progress=False
        )
        stats["label"] = label
        stats.pop("results", None)
        out.append(stats)
    return out
