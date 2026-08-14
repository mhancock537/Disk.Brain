"""Embeddings through Ollama, and the LanceDB vector store.

Vectors are L2-normalised on write, so a cosine comparison is a dot product and
the stored magnitudes cannot skew a ranking.

No ANN index is built. At roughly 45,000 vectors a flat scan is exact and takes
milliseconds; IVF or HNSW would trade recall away for a speedup nobody needs.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import Config, get_logger

log = get_logger("embed")

TABLE = "chunks"


def l2_normalise(vec: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return list(vec)
    return [v / norm for v in vec]


@dataclass
class Embedder:
    model: str
    batch_size: int = 32
    _dim: int | None = None

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._dim = len(self.encode(["dimension probe"])[0])
        return self._dim

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        """One Ollama call. Raises on failure so the caller decides about retry."""
        import ollama

        response = ollama.embed(
            model=self.model, input=list(texts), truncate=True, keep_alive="30m"
        )
        return [l2_normalise(v) for v in response.embeddings]

    def encode_batched(
        self, texts: Sequence[str], progress=None, task=None
    ) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            out.extend(self.encode(batch))
            if progress is not None and task is not None:
                progress.update(task, advance=len(batch))
        return out


def check_embed_model(cfg: Config) -> tuple[bool, str]:
    import ollama

    try:
        names = {m.model for m in ollama.list().models}
    except Exception as exc:
        return False, f"Ollama not reachable: {exc}"
    if cfg.embed.model in names:
        return True, cfg.embed.model
    return False, (
        f"embedding model {cfg.embed.model!r} is not pulled. "
        f"Run: ollama pull {cfg.embed.model}"
    )


# --- LanceDB -----------------------------------------------------------------


def _schema(dim: int):
    import pyarrow as pa

    return pa.schema(
        [
            pa.field("chunk_id", pa.string()),
            pa.field("concept_id", pa.string()),
            pa.field("source_hash", pa.string()),
            pa.field("source", pa.string()),
            pa.field("concept_type", pa.string()),
            pa.field("sensitivity", pa.string()),
            pa.field("title", pa.string()),
            pa.field("heading_path", pa.string()),
            pa.field("chunk_index", pa.int32()),
            pa.field("text", pa.string()),
            pa.field("tokens", pa.int32()),
            pa.field("vector", pa.list_(pa.float32(), dim)),
        ]
    )


_TABLE_CACHE: dict[str, Any] = {}


def open_table(cfg: Config, dim: int | None = None, create: bool = False):
    """Open the vector table, optionally creating it.

    The table is dropped and recreated on a rebuild rather than upserted: a
    changed embedding model changes the vector width, and a stale table of the
    wrong dimension is worse than no table.
    """
    import lancedb

    key = str(cfg.lance_dir)
    if not create and key in _TABLE_CACHE:
        return _TABLE_CACHE[key]

    cfg.lance_dir.mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(str(cfg.lance_dir))
    if create:
        _TABLE_CACHE.pop(key, None)
        if dim is None:
            raise ValueError("dim is required when creating the table")
        if TABLE in db.table_names():
            db.drop_table(TABLE)
        return db.create_table(TABLE, schema=_schema(dim))
    if TABLE not in db.table_names():
        raise FileNotFoundError(
            f"no vector table at {cfg.lance_dir}. Run `kb index` first."
        )
    table = db.open_table(TABLE)
    _TABLE_CACHE[key] = table
    return table


def embed_chunks(
    cfg: Config, chunks: list, show_progress: bool = True
) -> tuple[int, dict]:
    """Embed every chunk and write the LanceDB table. Returns (rows, stats)."""
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )

    ok, detail = check_embed_model(cfg)
    if not ok:
        raise RuntimeError(detail)

    embedder = Embedder(model=cfg.embed.model, batch_size=cfg.embed.batch_size)
    dim = embedder.dim
    table = open_table(cfg, dim=dim, create=True)

    t0 = time.monotonic()
    written = 0
    total_tokens = 0

    progress = Progress(
        TextColumn("[bold blue]embed"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("chunks"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        disable=not show_progress,
    )
    with progress:
        task = progress.add_task("embed", total=len(chunks))
        # Write in slabs so a long run holds a bounded amount in memory.
        slab = max(cfg.embed.batch_size * 8, 128)
        for start in range(0, len(chunks), slab):
            group = chunks[start : start + slab]
            vectors = embedder.encode_batched(
                [c.embed_input() for c in group], progress, task
            )
            rows = []
            for chunk, vector in zip(group, vectors):
                row = chunk.as_row()
                row["vector"] = vector
                rows.append(row)
                total_tokens += chunk.tokens
            table.add(rows)
            written += len(rows)

    seconds = time.monotonic() - t0
    return written, {
        "model": cfg.embed.model,
        "dimensions": dim,
        "vectors": written,
        "tokens": total_tokens,
        "seconds": round(seconds, 1),
        "chunks_per_second": round(written / seconds, 1) if seconds else 0.0,
    }


def dir_size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    if path.is_file():
        return path.stat().st_size / 1_048_576
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1_048_576
