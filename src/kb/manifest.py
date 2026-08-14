"""SQLite manifest plus the read-only filesystem crawler.

The crawler runs in two passes so the expensive one can be approved before it
runs:

  pass 1 (stat)  os.scandir only. Records path, size, mtime, inode, extension,
                 MIME and a scan_status. Touches no file contents.
  pass 2 (hash)  blake3 over everything that survived filtering, skipped when
                 (size, mtime, inode) are unchanged and a hash is already stored.

Both passes commit incrementally, so an interrupted run resumes where it
stopped rather than starting over.

Nothing in this module opens a source file for writing.
"""

from __future__ import annotations

import fnmatch
import mimetypes
import os
import sqlite3
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from blake3 import blake3

from .config import Config, get_logger
from .extract import route

log = get_logger("manifest")

HASH_CHUNK = 1 << 20  # 1 MiB
COMMIT_EVERY = 500

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path           TEXT PRIMARY KEY,
    root           TEXT NOT NULL,
    size           INTEGER NOT NULL,
    mtime          REAL NOT NULL,
    inode          INTEGER NOT NULL,
    ext            TEXT NOT NULL,
    mime           TEXT,
    hash           TEXT,
    scan_status    TEXT NOT NULL,
    extract_status TEXT NOT NULL DEFAULT 'pending',
    extract_path   TEXT,
    extract_engine TEXT,
    word_count     INTEGER,
    error          TEXT,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    run_id         TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_hash ON files(hash);
CREATE INDEX IF NOT EXISTS idx_files_scan ON files(scan_status);
CREATE INDEX IF NOT EXISTS idx_files_extract ON files(extract_status);
CREATE INDEX IF NOT EXISTS idx_files_ext ON files(ext);

CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    command     TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    stats       TEXT
);

-- One row per unique source document. `concept_id` is allocated once and then
-- frozen: it is the OKF concept ID, so re-enrichment that infers a different
-- type must not move the file and break every inbound link.
CREATE TABLE IF NOT EXISTS concepts (
    source_hash   TEXT PRIMARY KEY,
    concept_id    TEXT UNIQUE,
    concept_type  TEXT,
    title         TEXT,
    description   TEXT,
    tags          TEXT,       -- JSON array of strings
    entities      TEXT,       -- JSON array of {name, kind}
    sensitivity   TEXT,
    status        TEXT NOT NULL DEFAULT 'stable',
    enrich_status TEXT NOT NULL DEFAULT 'pending',
    error         TEXT,
    model         TEXT,
    generated_at  TEXT,
    ingest_run    TEXT,
    written_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_concepts_enrich ON concepts(enrich_status);
CREATE INDEX IF NOT EXISTS idx_concepts_type ON concepts(concept_type);
"""

# Magic-byte sniffing for the cases stdlib mimetypes cannot reach: no
# extension, or an extension that lies. Deliberately small; a full detector
# would be a new dependency.
MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),  # refined below for OOXML/EPUB
    (b"\xd0\xcf\x11\xe0", "application/x-ole-storage"),  # legacy .doc/.xls/.msg
    (b"{\\rtf", "application/rtf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF8", "image/gif"),
    (b"SQLite format 3\x00", "application/vnd.sqlite3"),
    (b"\x7fELF", "application/x-executable"),
    (b"\xcf\xfa\xed\xfe", "application/x-mach-binary"),
    (b"\x1f\x8b", "application/gzip"),
)

OOXML_HINT = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".epub": "application/epub+zip",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def sniff_mime(path: Path, ext: str) -> str | None:
    """Extension first, magic bytes as the tiebreaker. Read-only, 16 bytes."""
    guess, _ = mimetypes.guess_type(path.name)
    if guess:
        return guess
    try:
        with path.open("rb") as fh:
            head = fh.read(16)
    except OSError:
        return None
    for prefix, mime in MAGIC:
        if head.startswith(prefix):
            if mime == "application/zip" and ext in OOXML_HINT:
                return OOXML_HINT[ext]
            return mime
    if not head:
        return "application/x-empty"
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return "application/octet-stream"
    return "text/plain"


class DenyList:
    """fnmatch globs tested against basename, full path, and each component."""

    def __init__(self, globs: list[str]) -> None:
        self.globs = [g.lower() for g in globs]

    def match(self, path: Path, *, is_dir: bool) -> str | None:
        """Return the glob that rejected this path, or None."""
        low = str(path).lower()
        name = path.name.lower()
        parts = [p.lower() for p in path.parts]
        for g in self.globs:
            if "/" in g:
                if fnmatch.fnmatch(low, g) or fnmatch.fnmatch(low, g.rstrip("/*")):
                    return g
            else:
                if fnmatch.fnmatch(name, g):
                    return g
                if is_dir is False and any(fnmatch.fnmatch(p, g) for p in parts[:-1]):
                    return g
        return None


@dataclass
class StatRow:
    path: str
    root: str
    size: int
    mtime: float
    inode: int
    ext: str
    mime: str | None
    scan_status: str
    error: str | None = None


class Manifest:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Manifest:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- runs ---------------------------------------------------------------

    def start_run(self, command: str) -> str:
        run_id = new_run_id()
        self.conn.execute(
            "INSERT INTO runs (run_id, command, started_at) VALUES (?, ?, ?)",
            (run_id, command, utcnow()),
        )
        self.conn.commit()
        return run_id

    def finish_run(self, run_id: str, stats: str) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, stats = ? WHERE run_id = ?",
            (utcnow(), stats, run_id),
        )
        self.conn.commit()

    # --- stat pass ----------------------------------------------------------

    def upsert_stat(self, row: StatRow, run_id: str) -> None:
        """Insert or update one file.

        When (size, mtime, inode) are unchanged, hash and extraction state are
        preserved so a rerun costs nothing. When they change, both are cleared
        and the file re-enters the pipeline.
        """
        now = utcnow()
        self.conn.execute(
            """
            INSERT INTO files (path, root, size, mtime, inode, ext, mime,
                               scan_status, error, first_seen, last_seen, run_id)
            VALUES (:path, :root, :size, :mtime, :inode, :ext, :mime,
                    :scan_status, :error, :now, :now, :run_id)
            ON CONFLICT(path) DO UPDATE SET
                root        = excluded.root,
                size        = excluded.size,
                mtime       = excluded.mtime,
                inode       = excluded.inode,
                ext         = excluded.ext,
                mime        = excluded.mime,
                scan_status = excluded.scan_status,
                error       = excluded.error,
                last_seen   = excluded.last_seen,
                run_id      = excluded.run_id,
                hash = CASE
                    WHEN files.size = excluded.size
                     AND files.mtime = excluded.mtime
                     AND files.inode = excluded.inode
                    THEN files.hash ELSE NULL END,
                extract_status = CASE
                    WHEN files.size = excluded.size
                     AND files.mtime = excluded.mtime
                     AND files.inode = excluded.inode
                    THEN files.extract_status ELSE 'pending' END,
                extract_path = CASE
                    WHEN files.size = excluded.size
                     AND files.mtime = excluded.mtime
                     AND files.inode = excluded.inode
                    THEN files.extract_path ELSE NULL END
            """,
            {
                "path": row.path,
                "root": row.root,
                "size": row.size,
                "mtime": row.mtime,
                "inode": row.inode,
                "ext": row.ext,
                "mime": row.mime,
                "scan_status": row.scan_status,
                "error": row.error,
                "now": now,
                "run_id": run_id,
            },
        )

    def mark_missing(self, run_id: str, roots: list[str]) -> int:
        """Rows under a scanned root that this run did not see are gone."""
        total = 0
        for root in roots:
            cur = self.conn.execute(
                """
                UPDATE files SET scan_status = 'missing', last_seen = ?
                WHERE path LIKE ? AND run_id != ? AND scan_status != 'missing'
                """,
                (utcnow(), f"{root}%", run_id),
            )
            total += cur.rowcount
        self.conn.commit()
        return total

    # --- hash pass ----------------------------------------------------------

    def needs_hash(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT path, size FROM files "
            "WHERE scan_status = 'included' AND hash IS NULL ORDER BY size"
        ).fetchall()

    def set_hash(self, path: str, digest: str | None, error: str | None = None) -> None:
        if digest is None:
            self.conn.execute(
                "UPDATE files SET scan_status = 'unreadable', error = ? WHERE path = ?",
                (error, path),
            )
        else:
            self.conn.execute(
                "UPDATE files SET hash = ?, error = NULL WHERE path = ?", (digest, path)
            )

    # --- extraction ---------------------------------------------------------

    def pending_extractions(self) -> list[sqlite3.Row]:
        """One row per distinct hash: deduplicates identical files.

        MIN(path) makes the choice of representative deterministic.
        """
        return self.conn.execute(
            """
            SELECT hash, MIN(path) AS path, ext, mime, size, COUNT(*) AS copies
            FROM files
            WHERE scan_status = 'included'
              AND hash IS NOT NULL
              AND extract_status = 'pending'
            GROUP BY hash
            ORDER BY size
            """
        ).fetchall()

    def set_extract_result(
        self,
        file_hash: str,
        status: str,
        extract_path: str | None,
        engine: str | None,
        word_count: int | None,
        error: str | None,
    ) -> None:
        """Applies to every path sharing the hash, so dedup stays consistent."""
        self.conn.execute(
            """
            UPDATE files SET extract_status = ?, extract_path = ?, extract_engine = ?,
                             word_count = ?, error = ?
            WHERE hash = ?
            """,
            (status, extract_path, engine, word_count, error, file_hash),
        )

    # --- concepts (Phase 2) -------------------------------------------------

    def enrichable(self, recent_first: bool = False) -> list[sqlite3.Row]:
        """Documents with usable text that have no successful enrichment yet.

        Filtered on scan_status as well as extract_status: a file that later
        falls under a new deny glob becomes 'missing' while keeping its old
        'ok' extraction, and must not produce a concept.
        """
        return self.conn.execute(
            """
            SELECT f.hash, MIN(f.path) AS path, f.ext, f.extract_path, f.word_count,
                   f.root, c.concept_id, c.enrich_status
            FROM files f
            LEFT JOIN concepts c ON c.source_hash = f.hash
            WHERE f.scan_status = 'included'
              AND f.extract_status = 'ok'
              AND (c.enrich_status IS NULL OR c.enrich_status != 'ok')
            GROUP BY f.hash
            ORDER BY {order}
            """.format(
                # The drain works newest-first: a file the user just changed
                # should not wait behind an arbitrary backlog. A bulk pass uses
                # hash order instead, which is stable and uncorrelated with
                # size, so `--limit N` samples the corpus fairly.
                order="MAX(f.last_seen) DESC, f.hash" if recent_first else "f.hash"
            )
        ).fetchall()

    # Ordered by hash, which is stable across runs and uncorrelated with size.
    # Ordering by word_count would make `--limit N` sample only the largest
    # documents and would give the progress bar a badly skewed time estimate.

    def taken_concept_ids(self) -> set[str]:
        return {
            r[0]
            for r in self.conn.execute(
                "SELECT concept_id FROM concepts WHERE concept_id IS NOT NULL"
            )
        }

    def existing_concept_id(self, source_hash: str) -> str | None:
        row = self.conn.execute(
            "SELECT concept_id FROM concepts WHERE source_hash = ?", (source_hash,)
        ).fetchone()
        return row["concept_id"] if row else None

    def save_concept(self, rec: dict) -> None:
        """Upsert enrichment output. concept_id is written once and never moved."""
        self.conn.execute(
            """
            INSERT INTO concepts (source_hash, concept_id, concept_type, title,
                                  description, tags, entities, sensitivity, status,
                                  enrich_status, error, model, generated_at, ingest_run)
            VALUES (:source_hash, :concept_id, :concept_type, :title, :description,
                    :tags, :entities, :sensitivity, :status, :enrich_status, :error,
                    :model, :generated_at, :ingest_run)
            ON CONFLICT(source_hash) DO UPDATE SET
                concept_id    = COALESCE(concepts.concept_id, excluded.concept_id),
                concept_type  = excluded.concept_type,
                title         = excluded.title,
                description   = excluded.description,
                tags          = excluded.tags,
                entities      = excluded.entities,
                sensitivity   = excluded.sensitivity,
                status        = excluded.status,
                enrich_status = excluded.enrich_status,
                error         = excluded.error,
                model         = excluded.model,
                generated_at  = excluded.generated_at,
                ingest_run    = excluded.ingest_run
            """,
            rec,
        )

    def mark_written(self, source_hash: str) -> None:
        self.conn.execute(
            "UPDATE concepts SET written_at = ? WHERE source_hash = ?",
            (utcnow(), source_hash),
        )

    def enriched_concepts(self) -> list[sqlite3.Row]:
        """Every successfully enriched concept, joined to its source file."""
        return self.conn.execute(
            """
            SELECT c.*, f.path AS source_path, f.ext, f.size, f.mtime,
                   f.extract_path, f.word_count
            FROM concepts c
            JOIN (SELECT hash, MIN(path) AS path, ext, size, mtime,
                         extract_path, word_count
                  FROM files WHERE scan_status = 'included' GROUP BY hash) f
              ON f.hash = c.source_hash
            WHERE c.enrich_status = 'ok' AND c.concept_id IS NOT NULL
            ORDER BY c.concept_id
            """
        ).fetchall()

    def commit(self) -> None:
        self.conn.commit()


# --- crawler -----------------------------------------------------------------


def walk_root(
    root: Path, deny: DenyList, cfg: Config
) -> Iterator[tuple[Path, os.stat_result | None, str, str | None]]:
    """Yield (path, stat, scan_status, error) for every file beneath `root`.

    Directories are pruned on match, so a denied tree is never descended.
    """
    try:
        root_dev = root.stat().st_dev
    except OSError as exc:
        log.error("root unreadable: %s (%s)", root, exc)
        return

    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except PermissionError as exc:
            log.warning("permission denied: %s (%s)", current, exc)
            continue
        except OSError as exc:
            log.warning("unreadable directory: %s (%s)", current, exc)
            continue

        for entry in entries:
            path = Path(entry.path)
            try:
                is_dir = entry.is_dir(follow_symlinks=cfg.follow_symlinks)
                is_file = entry.is_file(follow_symlinks=cfg.follow_symlinks)
                is_link = entry.is_symlink()
            except OSError as exc:
                yield path, None, "unreadable", str(exc)
                continue

            if is_link and not cfg.follow_symlinks:
                continue

            hit = deny.match(path, is_dir=is_dir)
            if hit:
                if not is_dir:
                    yield path, None, "denied", hit
                continue

            if is_dir:
                if cfg.same_device_only:
                    try:
                        if entry.stat(follow_symlinks=False).st_dev != root_dev:
                            continue
                    except OSError:
                        continue
                stack.append(path)
                continue

            if not is_file:
                continue

            try:
                st = entry.stat(follow_symlinks=cfg.follow_symlinks)
            except OSError as exc:
                yield path, None, "unreadable", str(exc)
                continue

            # A dataless (cloud-evicted) file would trigger a download on read.
            # SF_DATALESS = 0x40000000 in st_flags. Skip rather than force it.
            if getattr(st, "st_flags", 0) & 0x40000000:
                yield path, st, "dataless", "cloud-evicted, not materialised"
                continue

            if st.st_size > cfg.max_file_bytes:
                yield path, st, "too_large", None
                continue

            yield path, st, "included", None


def scan(cfg: Config, mf: Manifest, do_hash: bool = True) -> dict[str, int]:
    """Pass 1 (stat) and optionally pass 2 (hash). Commits incrementally."""
    deny = DenyList(cfg.deny_globs)
    run_id = mf.start_run("scan")
    counts: dict[str, int] = {}
    seen = 0
    t0 = time.monotonic()

    roots = cfg.enabled_roots()
    for root in roots:
        if not root.path.is_dir():
            log.warning("root missing, skipped: %s", root.path)
            continue
        log.info("scanning %s", root.path)
        for path, st, status, error in walk_root(root.path, deny, cfg):
            ext = path.suffix.lower()
            mime = sniff_mime(path, ext) if status == "included" else None
            # No extractor can read it, so do not spend blake3 on it. The row
            # is still recorded: the manifest stays a complete census.
            if status == "included" and route(ext, mime, cfg) == "skip":
                status = "no_route"
            mf.upsert_stat(
                StatRow(
                    path=str(path),
                    root=str(root.path),
                    size=st.st_size if st else 0,
                    mtime=st.st_mtime if st else 0.0,
                    inode=st.st_ino if st else 0,
                    ext=ext,
                    mime=mime,
                    scan_status=status,
                    error=error,
                ),
                run_id,
            )
            counts[status] = counts.get(status, 0) + 1
            seen += 1
            if seen % COMMIT_EVERY == 0:
                mf.commit()
                log.info("  %d entries recorded", seen)
    mf.commit()

    counts["missing"] = mf.mark_missing(run_id, [str(r.path) for r in roots])
    counts["stat_seconds"] = round(time.monotonic() - t0, 1)

    if do_hash:
        counts.update(hash_pass(mf))

    import json

    mf.finish_run(run_id, json.dumps(counts))
    return counts


def hash_pass(mf: Manifest) -> dict[str, int]:
    """blake3 every included file lacking a hash. Resumable at any point."""
    todo = mf.needs_hash()
    log.info("hashing %d files", len(todo))
    t0 = time.monotonic()
    done = failed = 0
    total_bytes = 0
    for i, row in enumerate(todo, 1):
        p = Path(row["path"])
        try:
            hasher = blake3()
            with p.open("rb") as fh:
                while chunk := fh.read(HASH_CHUNK):
                    hasher.update(chunk)
            mf.set_hash(row["path"], hasher.hexdigest())
            total_bytes += row["size"]
            done += 1
        except OSError as exc:
            mf.set_hash(row["path"], None, str(exc))
            failed += 1
            log.warning("hash failed: %s (%s)", p, exc)
        if i % COMMIT_EVERY == 0:
            mf.commit()
            log.info("  hashed %d/%d", i, len(todo))
    mf.commit()
    return {
        "hashed": done,
        "hash_failed": failed,
        "hashed_bytes": total_bytes,
        "hash_seconds": round(time.monotonic() - t0, 1),
    }
