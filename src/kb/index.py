"""`kb index`: rebuild the vector store and the BM25 index from the bundle.

The bundle drives the work. It says which concepts exist, and every concept's
frontmatter carries `source_hash`, which locates the extracted body text under
`data/extracted/`.

That split is deliberate. Chunking only the concept files would give one short
record per document and roughly 2,500 chunks; the brief's own estimate of 40,000
to 60,000 chunks is only reachable from full document text. So the bundle is the
index of record and the extraction holds the prose. Both are rebuildable: the
bundle from the manifest, the extraction from the source files.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .chunk import Chunk, chunk_document
from .config import Config, get_logger
from .embed import dir_size_mb, embed_chunks
from .okf import RESERVED, concept_id_for, parse

log = get_logger("index")

FTS_SCHEMA = """
DROP TABLE IF EXISTS chunks;
CREATE VIRTUAL TABLE chunks USING fts5(
    chunk_id UNINDEXED,
    concept_id UNINDEXED,
    source UNINDEXED,
    concept_type UNINDEXED,
    sensitivity UNINDEXED,
    title,
    heading_path,
    text,
    tokenize = 'porter unicode61 remove_diacritics 2'
);
"""


@dataclass
class ConceptSource:
    concept_id: str
    concept_type: str
    title: str
    description: str
    sensitivity: str
    source_hash: str
    source: str
    status: str
    text_path: Path | None


def extract_path_for(cfg: Config, source_hash: str) -> Path | None:
    p = cfg.extract_out_dir / source_hash[:2] / f"{source_hash}.md"
    return p if p.is_file() else None


def read_bundle(cfg: Config) -> tuple[list[ConceptSource], list[str]]:
    """Walk the bundle and pair every concept with its extracted body text."""
    out: list[ConceptSource] = []
    problems: list[str] = []
    bundle = cfg.bundle_dir
    if not bundle.is_dir():
        return out, [f"no bundle at {bundle}"]

    for path in sorted(bundle.rglob("*.md")):
        # RESERVED, not a hardcoded pair. entity-review.md is a report `kb
        # graph` writes into the bundle, and parsing it as a concept logged
        # "unparseable concept" on every index run: noise indistinguishable
        # from a real corruption warning, which is what made it worth removing.
        if path.name in RESERVED:
            continue
        doc = parse(path.read_text(encoding="utf-8", errors="replace"))
        if doc is None or not doc.type:
            problems.append(f"unparseable concept: {path.name}")
            continue

        fm = doc.frontmatter
        status = str(fm.get("status") or "stable")
        if status == "deprecated":
            # §5.4 keeps deprecated concepts for links and history, but they
            # are not current, so they stay out of retrieval.
            continue

        source_hash = str(fm.get("source_hash") or "")
        text_path = extract_path_for(cfg, source_hash) if source_hash else None
        if text_path is None:
            problems.append(f"no extracted text for {path.name}")

        out.append(
            ConceptSource(
                concept_id=concept_id_for(path, bundle),
                concept_type=doc.type,
                title=str(fm.get("title") or path.stem),
                description=str(fm.get("description") or ""),
                sensitivity=str(fm.get("sensitivity") or "unknown"),
                source_hash=source_hash,
                source=str(fm.get("source") or "local"),
                status=status,
                text_path=text_path,
            )
        )
    return out, problems


def build_chunks(cfg: Config, concepts: list[ConceptSource]) -> list[Chunk]:
    chunks: list[Chunk] = []
    for c in concepts:
        text = ""
        if c.text_path is not None:
            text = c.text_path.read_text(encoding="utf-8", errors="replace")
        chunks.extend(
            chunk_document(
                concept_id=c.concept_id,
                source_hash=c.source_hash,
                concept_type=c.concept_type,
                sensitivity=c.sensitivity,
                title=c.title,
                text=text,
                summary=c.description,
                source=c.source,
                max_tokens=cfg.chunk.max_tokens,
                overlap_ratio=cfg.chunk.overlap_ratio,
            )
        )
    return chunks


def delete_from_indexes(cfg: Config, concept_ids: list[str]) -> dict[str, int]:
    """Remove every chunk of the named concepts from both stores.

    Both are keyed by concept_id, so an update is delete-then-insert rather
    than a full rebuild. A full rebuild embeds 35,000 chunks and takes about
    50 minutes, which is not a per-file-change operation.
    """
    if not concept_ids:
        return {"lance": 0, "fts": 0}
    listed = ", ".join("'" + c.replace("'", "''") + "'" for c in concept_ids)
    removed = {"lance": 0, "fts": 0}

    if cfg.lance_dir.exists():
        from .embed import open_table

        try:
            table = open_table(cfg)
            before = table.count_rows()
            table.delete(f"concept_id IN ({listed})")
            removed["lance"] = before - table.count_rows()
        except FileNotFoundError:
            pass

    if cfg.fts_path.is_file():
        conn = sqlite3.connect(cfg.fts_path)
        try:
            cur = conn.execute(f"DELETE FROM chunks WHERE concept_id IN ({listed})")
            removed["fts"] = cur.rowcount
            conn.commit()
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()
    return removed


def add_to_indexes(cfg: Config, chunks: list[Chunk]) -> dict[str, int]:
    """Append chunks to both stores without touching anything already there."""
    if not chunks:
        return {"lance": 0, "fts": 0}

    from .embed import Embedder, check_embed_model, open_table

    ok, detail = check_embed_model(cfg)
    if not ok:
        raise RuntimeError(detail)

    embedder = Embedder(model=cfg.embed.model, batch_size=cfg.embed.batch_size)
    table = open_table(cfg, dim=embedder.dim, create=not cfg.lance_dir.exists())
    vectors = embedder.encode_batched([c.embed_input() for c in chunks])
    rows = []
    for chunk, vector in zip(chunks, vectors):
        row = chunk.as_row()
        row["vector"] = vector
        rows.append(row)
    table.add(rows)

    conn = sqlite3.connect(cfg.fts_path)
    try:
        conn.executescript(FTS_SCHEMA.split("DROP TABLE IF EXISTS chunks;")[1]
                           .replace("CREATE VIRTUAL TABLE chunks",
                                    "CREATE VIRTUAL TABLE IF NOT EXISTS chunks"))
        conn.executemany(
            "INSERT INTO chunks (chunk_id, concept_id, source, concept_type, "
            "sensitivity, title, heading_path, text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(c.chunk_id, c.concept_id, c.source, c.concept_type, c.sensitivity,
              c.title, c.heading_path, c.text) for c in chunks],
        )
        conn.commit()
    finally:
        conn.close()
    return {"lance": len(rows), "fts": len(rows)}


def update_index(cfg: Config, concept_ids: list[str]) -> dict:
    """Refresh only the named concepts in both indexes.

    Concepts that no longer exist in the bundle, or that are now deprecated,
    are removed and not re-added.
    """
    concept_ids = sorted(set(concept_ids))
    if not concept_ids:
        return {"concepts": 0, "removed": {"lance": 0, "fts": 0},
                "added": {"lance": 0, "fts": 0}}

    removed = delete_from_indexes(cfg, concept_ids)

    live, _problems = read_bundle(cfg)
    wanted = [c for c in live if c.concept_id in set(concept_ids)]
    chunks = build_chunks(cfg, wanted)
    added = add_to_indexes(cfg, chunks)

    return {
        "concepts": len(concept_ids),
        "reindexed": len(wanted),
        "dropped": len(concept_ids) - len(wanted),
        "removed": removed,
        "added": added,
        "chunks": len(chunks),
    }


def build_fts(cfg: Config, chunks: list[Chunk]) -> int:
    """Rebuild the FTS5 table. Dropped and recreated, never incrementally patched."""
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.fts_path)
    try:
        conn.executescript(FTS_SCHEMA)
        conn.executemany(
            "INSERT INTO chunks (chunk_id, concept_id, source, concept_type, "
            "sensitivity, title, heading_path, text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    c.chunk_id, c.concept_id, c.source, c.concept_type,
                    c.sensitivity, c.title, c.heading_path, c.text,
                )
                for c in chunks
            ],
        )
        conn.execute("INSERT INTO chunks(chunks) VALUES('optimize')")
        conn.commit()
        return conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    finally:
        conn.close()


def run_index(cfg: Config, show_progress: bool = True) -> dict:
    """Rebuild both indexes from the bundle. Reports real numbers, not estimates."""
    t0 = time.monotonic()

    concepts, problems = read_bundle(cfg)
    if not concepts:
        raise RuntimeError(f"no concepts found in {cfg.bundle_dir}. Run `kb bundle`.")
    for p in problems[:10]:
        log.warning("%s", p)

    t_chunk = time.monotonic()
    chunks = build_chunks(cfg, concepts)
    chunk_seconds = time.monotonic() - t_chunk
    if not chunks:
        raise RuntimeError("chunking produced nothing")

    vectors, embed_stats = embed_chunks(cfg, chunks, show_progress=show_progress)

    t_fts = time.monotonic()
    fts_rows = build_fts(cfg, chunks)
    fts_seconds = time.monotonic() - t_fts

    tokens = [c.tokens for c in chunks]
    by_source: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for c in chunks:
        by_source[c.source] = by_source.get(c.source, 0) + 1
        by_type[c.concept_type] = by_type.get(c.concept_type, 0) + 1

    return {
        "concepts": len(concepts),
        "concepts_without_text": sum(1 for c in concepts if c.text_path is None),
        "chunks": len(chunks),
        "chunks_per_concept": round(len(chunks) / len(concepts), 1),
        "tokens_total": sum(tokens),
        "tokens_mean": round(sum(tokens) / len(tokens), 1),
        "tokens_max": max(tokens),
        "chunk_seconds": round(chunk_seconds, 1),
        "vectors": vectors,
        "embed": embed_stats,
        "fts_rows": fts_rows,
        "fts_seconds": round(fts_seconds, 1),
        "by_source": by_source,
        "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        "disk_mb": {
            "lance": round(dir_size_mb(cfg.lance_dir), 1),
            "fts": round(dir_size_mb(cfg.fts_path), 1),
            "extracted": round(dir_size_mb(cfg.extract_out_dir), 1),
        },
        "total_seconds": round(time.monotonic() - t0, 1),
        "problems": problems[:20],
    }
