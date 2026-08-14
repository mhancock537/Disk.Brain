"""Drives extraction across the manifest: resumable, per-file error isolation.

Work is keyed by blake3 hash, not by path, so identical files are extracted
once and the result is applied to every path that shares the hash.
"""

from __future__ import annotations

import time
from pathlib import Path

from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from ..config import Config, get_logger
from ..manifest import Manifest
from . import ExtractResult, extract_file, extract_out_path

log = get_logger("extract.runner")

COMMIT_EVERY = 25


def _write_extract(cfg: Config, file_hash: str, result: ExtractResult, src: Path) -> Path:
    out = extract_out_path(cfg, file_hash)
    out.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "<!-- okf-kb extraction",
        f"source: {src}",
        f"hash: {file_hash}",
        f"engine: {result.engine}",
    ]
    for k, v in sorted(result.meta.items()):
        header.append(f"{k}: {v}")
    header.append("-->")
    out.write_text("\n".join(header) + "\n\n" + result.text, encoding="utf-8")
    return out


def run_extract(
    cfg: Config,
    mf: Manifest,
    limit: int | None = None,
    only_ext: str | None = None,
    show_progress: bool = True,
) -> dict[str, int]:
    todo = mf.pending_extractions()
    if only_ext:
        want = only_ext if only_ext.startswith(".") else f".{only_ext}"
        todo = [r for r in todo if r["ext"] == want.lower()]
    if limit:
        todo = todo[:limit]

    run_id = mf.start_run("extract")
    stats = {"ok": 0, "empty": 0, "failed": 0, "skipped": 0}
    words = 0
    t0 = time.monotonic()

    progress = Progress(
        TextColumn("[bold blue]extract"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("{task.fields[name]}"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        disable=not show_progress,
    )

    with progress:
        task = progress.add_task("extract", total=len(todo), name="")
        for i, row in enumerate(todo, 1):
            src = Path(row["path"])
            progress.update(task, advance=1, name=src.name[:44])

            if not src.exists():
                mf.set_extract_result(
                    row["hash"], "failed", None, None, None, "source vanished"
                )
                stats["failed"] += 1
                continue

            result = extract_file(src, row["ext"], row["mime"], cfg)

            if result.status == "ok" and len(result.text) < cfg.extract_min_chars:
                result.status = "empty"

            if result.status == "ok":
                out = _write_extract(cfg, row["hash"], result, src)
                mf.set_extract_result(
                    row["hash"], "ok", str(out), result.engine,
                    result.word_count, None,
                )
                words += result.word_count
            else:
                mf.set_extract_result(
                    row["hash"], result.status, None, result.engine or None,
                    None, result.error,
                )
            stats[result.status] = stats.get(result.status, 0) + 1

            if i % COMMIT_EVERY == 0:
                mf.commit()

    mf.commit()
    stats["total_words"] = words
    stats["seconds"] = round(time.monotonic() - t0, 1)

    import json

    mf.finish_run(run_id, json.dumps(stats))
    return stats
