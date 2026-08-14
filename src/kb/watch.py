"""Incremental updates: an FSEvents watcher, and a scheduled drain.

The split is deliberate. The watcher does only cheap work inline, so it can keep
up with a burst: stat, hash, extract, and deprecate. Enrichment and embedding are
expensive (about 17 seconds per document on the local model) and would seize the
GPU for hours if a git checkout or a cloud sync touched a thousand files, so they
are queued and drained on a schedule with a cap.

The queue is not a new structure. The manifest already models it:
`files.extract_status = 'pending'` is the extraction queue and
`concepts.enrich_status != 'ok'` is the enrichment queue. A changed file clears
its hash, which resets both, and the work reappears on its own.

Watching is wider than indexing. One FSEvents stream covers the whole home
directory, but a path only enters the pipeline if it passes the denylist and
sits under an enabled scan root. Widening the corpus stays an explicit edit to
`[[scan.roots]]`.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .config import Config, get_logger
from .extract import route
from .manifest import DenyList, Manifest, StatRow, sniff_mime

log = get_logger("watch")

# FSEvents coalesces, but an editor save still emits several events for one
# file. Nothing is acted on until a path has been quiet for this long.
POLL_SECONDS = 1.0


@dataclass
class Pending:
    path: Path
    last_seen: float
    deleted: bool = False


class ChangeQueue:
    """Debounced set of paths waiting to settle. Thread-safe."""

    def __init__(self, debounce_seconds: float, max_size: int) -> None:
        self.debounce = debounce_seconds
        self.max_size = max_size
        self._items: dict[str, Pending] = {}
        self._lock = threading.Lock()
        self.dropped = 0

    def add(self, path: Path, deleted: bool = False) -> None:
        with self._lock:
            key = str(path)
            if key not in self._items and len(self._items) >= self.max_size:
                self.dropped += 1
                if self.dropped % 500 == 1:
                    log.warning(
                        "change queue is full at %d, dropping events (%d dropped). "
                        "Run `kb scan` to catch up.",
                        self.max_size, self.dropped,
                    )
                return
            self._items[key] = Pending(path, time.monotonic(), deleted)

    def take_settled(self) -> list[Pending]:
        now = time.monotonic()
        with self._lock:
            ready = [p for p in self._items.values() if now - p.last_seen >= self.debounce]
            for p in ready:
                self._items.pop(str(p.path), None)
        return ready

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


def in_scope(cfg: Config, deny: DenyList, path: Path) -> bool:
    """Does this path belong in the corpus?

    Three gates: not denied, under an enabled scan root when that is required,
    and something an extractor can actually read.
    """
    if deny.match(path, is_dir=False):
        return False
    if cfg.watch.require_configured_root:
        roots = [str(r.path) for r in cfg.enabled_roots()]
        if not any(str(path) == r or str(path).startswith(r + "/") for r in roots):
            return False
    ext = path.suffix.lower()
    return route(ext, None, cfg) != "skip"


def process_path(cfg: Config, mf: Manifest, pending: Pending, run_id: str) -> str:
    """Handle one settled path. Returns what happened, for the counters."""
    from .extract import extract_file, extract_out_path

    path = pending.path

    if not path.exists():
        cur = mf.conn.execute(
            "UPDATE files SET scan_status = 'missing' WHERE path = ? "
            "AND scan_status != 'missing'",
            (str(path),),
        )
        mf.commit()
        return "deleted" if cur.rowcount else "ignored"

    deny = DenyList(cfg.deny_globs)
    if not in_scope(cfg, deny, path):
        return "ignored"

    try:
        st = path.stat()
    except OSError:
        return "ignored"
    if st.st_size > cfg.max_file_bytes:
        return "too_large"

    ext = path.suffix.lower()
    before = mf.conn.execute(
        "SELECT hash, extract_status FROM files WHERE path = ?", (str(path),)
    ).fetchone()

    mf.upsert_stat(
        StatRow(
            path=str(path),
            root=_root_for(cfg, path),
            size=st.st_size,
            mtime=st.st_mtime,
            inode=st.st_ino,
            ext=ext,
            mime=sniff_mime(path, ext),
            scan_status="included",
        ),
        run_id,
    )
    mf.commit()

    after = mf.conn.execute(
        "SELECT hash FROM files WHERE path = ?", (str(path),)
    ).fetchone()
    if before and after and before["hash"] and after["hash"] == before["hash"]:
        return "unchanged"

    # Hash it, then extract. Both are cheap enough to run inline.
    from blake3 import blake3

    try:
        hasher = blake3()
        with path.open("rb") as fh:
            while chunk := fh.read(1 << 20):
                hasher.update(chunk)
        digest = hasher.hexdigest()
    except OSError as exc:
        mf.set_hash(str(path), None, str(exc))
        mf.commit()
        return "unreadable"
    mf.set_hash(str(path), digest)
    mf.commit()

    already = mf.conn.execute(
        "SELECT extract_status FROM files WHERE hash = ? AND extract_status = 'ok' "
        "LIMIT 1", (digest,)
    ).fetchone()
    if already:
        # A move, or a duplicate of something already extracted. The concept
        # keeps its frozen ID because that is keyed on the hash.
        return "known_hash"

    result = extract_file(path, ext, sniff_mime(path, ext), cfg)
    if result.status == "ok" and len(result.text) >= cfg.extract_min_chars:
        out = extract_out_path(cfg, digest)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            f"<!-- okf-kb extraction\nsource: {path}\nhash: {digest}\n"
            f"engine: {result.engine}\n-->\n\n{result.text}",
            encoding="utf-8",
        )
        mf.set_extract_result(digest, "ok", str(out), result.engine,
                              result.word_count, None)
        mf.commit()
        return "extracted"

    mf.set_extract_result(digest, result.status, None, result.engine or None,
                          None, result.error)
    mf.commit()
    return result.status


def _root_for(cfg: Config, path: Path) -> str:
    for r in cfg.enabled_roots():
        if str(path).startswith(str(r.path) + "/"):
            return str(r.path)
    return str(cfg.watch.root)


# --- the watcher -------------------------------------------------------------


def run_watch(cfg: Config, once_seconds: float | None = None) -> dict[str, Any]:
    """Watch the configured root until interrupted. Returns run counters."""
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    deny = DenyList(cfg.deny_globs)
    queue = ChangeQueue(cfg.watch.debounce_seconds, cfg.watch.max_queue)
    counters: dict[str, int] = {}

    class Handler(FileSystemEventHandler):
        def on_any_event(self, event) -> None:
            if event.is_directory:
                return
            for attr in ("src_path", "dest_path"):
                raw = getattr(event, attr, None)
                if not raw:
                    continue
                path = Path(raw if isinstance(raw, str) else raw.decode())
                # Cheap pre-filter so a node_modules install does not fill the
                # queue with work that would be discarded later anyway.
                if deny.match(path, is_dir=False):
                    continue
                queue.add(path, deleted=event.event_type == "deleted")

    observer = Observer()
    observer.schedule(Handler(), str(cfg.watch.root), recursive=True)
    observer.start()
    log.info(
        "watching %s (debounce %.1fs, indexing limited to %d configured roots)",
        cfg.watch.root, cfg.watch.debounce_seconds, len(cfg.enabled_roots()),
    )

    started = time.monotonic()
    mf = Manifest(cfg.manifest_path)
    run_id = mf.start_run("watch")
    try:
        while True:
            time.sleep(POLL_SECONDS)
            for pending in queue.take_settled():
                try:
                    outcome = process_path(cfg, mf, pending, run_id)
                except Exception as exc:  # one bad file never stops the watcher
                    log.warning("watch failed on %s (%s)", pending.path, exc)
                    outcome = "error"
                counters[outcome] = counters.get(outcome, 0) + 1
                if outcome not in ("ignored", "unchanged"):
                    log.info("%-11s %s", outcome, pending.path)
            if once_seconds is not None and time.monotonic() - started >= once_seconds:
                break
    except KeyboardInterrupt:
        log.info("stopping watcher")
    finally:
        observer.stop()
        observer.join(timeout=5)
        counters["dropped_events"] = queue.dropped
        mf.finish_run(run_id, json.dumps(counters))
        mf.close()
    return counters


# --- the drain ---------------------------------------------------------------


def run_drain(cfg: Config, limit: int | None = None,
              show_progress: bool = True) -> dict[str, Any]:
    """Do the expensive queued work: enrich, rewrite, reindex, rebuild.

    Capped, so a burst of file changes costs a bounded amount of GPU per run
    rather than seizing the machine. Whatever is left stays queued.
    """
    from .bundle import deprecate_missing, write_bundle
    from .enrich import run_enrich
    from .graph import build_graph
    from .index import update_index
    from .okf import prepend_log_entry

    # `is None`, not truthiness: `--limit 0` means enrich nothing this run,
    # and falling through to the configured cap would spend hours of GPU that
    # the caller explicitly declined.
    cap = cfg.drain.max_documents if limit is None else max(0, int(limit))
    t0 = time.monotonic()
    stats: dict[str, Any] = {"cap": cap}

    with Manifest(cfg.manifest_path) as mf:
        pending = len(mf.enrichable(recent_first=True))
        stats["queued_before"] = pending

        deprecated = deprecate_missing(cfg, mf)
        stats["deprecated"] = deprecated["deprecated"]

        if pending and cap > 0:
            stats["enrich"] = run_enrich(
                cfg, mf, limit=cap, show_progress=show_progress, recent_first=True
            )
        else:
            stats["enrich"] = {"ok": 0, "failed": 0}

        touched = {
            r["concept_id"]
            for r in mf.conn.execute(
                "SELECT concept_id FROM concepts WHERE enrich_status = 'ok' "
                "AND (written_at IS NULL OR written_at < generated_at)"
            ).fetchall()
            if r["concept_id"]
        }
        touched.update(deprecated["concept_ids"])

        if touched:
            bundle_stats, report = write_bundle(cfg, mf, show_progress=show_progress)
            stats["bundle"] = {
                "concepts": bundle_stats["concepts"],
                "validation": bundle_stats["validation"],
            }
            stats["index"] = update_index(cfg, sorted(touched))
        else:
            stats["bundle"] = {"concepts": 0}
            stats["index"] = {"concepts": 0}

        stats["queued_after"] = len(mf.enrichable(recent_first=True))

    if touched and cfg.drain.rebuild_graph:
        # A full rebuild rather than a patch. It is a bulk Parquet load
        # measured at 0.46 s for 22 concepts, and a rebuild cannot drift.
        stats["graph"] = build_graph(cfg)["nodes"]

    stats["seconds"] = round(time.monotonic() - t0, 1)

    if touched or stats["deprecated"]:
        prepend_log_entry(
            cfg.bundle_dir / "log.md",
            date.today(),
            [
                f"* **Update**: drain touched {len(touched)} concepts, "
                f"enriched {stats['enrich'].get('ok', 0)}, "
                f"deprecated {stats['deprecated']}, "
                f"{stats['queued_after']} still queued."
            ],
            heading="Bundle Update Log",
        )
    return stats
