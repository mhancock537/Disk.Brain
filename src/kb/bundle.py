"""Turn enriched records into an OKF bundle on disk.

Writes one concept per source document, a `# Related` cross-link section, an
`index.md` per directory, and a root `log.md`. Every write is atomic, and the
whole bundle is validated before the command reports success.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config, get_logger
from .manifest import Manifest
from .okf import (
    OKF_VERSION,
    RESERVED,
    parse,
    Document,
    IndexEntry,
    ValidationReport,
    concept_id_for,
    link_target,
    prepend_log_entry,
    render,
    render_index,
    slugify,
    utc_now_iso,
    validate_bundle,
    write_atomic,
)

log = get_logger("bundle")

# An entity match is a stronger signal of relatedness than a shared topic tag,
# so it counts for more when scoring cross-link candidates.
ENTITY_WEIGHT = 1.5
TAG_WEIGHT = 1.0


@dataclass
class Concept:
    source_hash: str
    concept_id: str
    concept_type: str
    title: str
    description: str
    tags: list[str]
    entities: list[dict[str, str]]
    sensitivity: str
    status: str
    model: str
    generated_at: str
    ingest_run: str
    source_path: str
    ext: str
    size: int
    mtime: float
    word_count: int

    @property
    def directory(self) -> str:
        return self.concept_id.rsplit("/", 1)[0] if "/" in self.concept_id else ""

    def terms(self) -> list[tuple[str, str, float]]:
        """(kind, normalised key, weight) for every linkable term."""
        out = [("tag", t.casefold(), TAG_WEIGHT) for t in self.tags]
        out += [
            ("entity", f"{e['kind']}:{e['name'].casefold()}", ENTITY_WEIGHT)
            for e in self.entities
        ]
        return out


def load_concepts(mf: Manifest) -> list[Concept]:
    out: list[Concept] = []
    for r in mf.enriched_concepts():
        out.append(
            Concept(
                source_hash=r["source_hash"],
                concept_id=r["concept_id"],
                concept_type=r["concept_type"] or "Other",
                title=r["title"] or Path(r["source_path"]).stem,
                description=r["description"] or "",
                tags=json.loads(r["tags"] or "[]"),
                entities=json.loads(r["entities"] or "[]"),
                sensitivity=r["sensitivity"] or "personal",
                status=r["status"] or "stable",
                model=r["model"] or "",
                generated_at=r["generated_at"] or utc_now_iso(),
                ingest_run=r["ingest_run"] or "",
                source_path=r["source_path"],
                ext=r["ext"] or "",
                size=r["size"] or 0,
                mtime=r["mtime"] or 0.0,
                word_count=r["word_count"] or 0,
            )
        )
    return out


# --- cross-links -------------------------------------------------------------


def related_map(cfg: Config, concepts: list[Concept]) -> dict[str, list[tuple[str, float]]]:
    """Score cross-link candidates through an inverted index.

    Iterating concept pairs would be 3.3 million comparisons at this corpus
    size. Posting lists reach only the concepts that actually share a term.

    A term's weight falls as it gets more common, and a term appearing in more
    than `link_rarity_ceiling` concepts is dropped: a tag on 800 documents says
    nothing about any particular pair.
    """
    postings: dict[str, set[str]] = defaultdict(set)
    weights: dict[str, float] = {}
    by_id = {c.concept_id: c for c in concepts}

    for c in concepts:
        for _kind, key, weight in c.terms():
            postings[key].add(c.concept_id)
            weights[key] = weight

    scores: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for key, members in postings.items():
        df = len(members)
        if df < 2 or df > cfg.bundle.link_rarity_ceiling:
            continue
        rarity = 1.0 / math.log2(df + 1)
        contribution = weights[key] * rarity
        members_list = sorted(members)
        for a in members_list:
            for b in members_list:
                if a != b:
                    scores[a][b] += contribution

    out: dict[str, list[tuple[str, float]]] = {}
    for cid, peers in scores.items():
        ranked = sorted(peers.items(), key=lambda kv: (-kv[1], kv[0]))
        kept = [
            (peer, round(score, 3))
            for peer, score in ranked
            if score >= cfg.bundle.min_link_score and peer in by_id
        ][: cfg.bundle.max_related]
        if kept:
            out[cid] = kept
    return out


# --- concept rendering -------------------------------------------------------


def build_document(
    cfg: Config, c: Concept, related: list[tuple[str, float]], by_id: dict[str, Concept]
) -> Document:
    source = Path(c.source_path)
    resource = source.as_uri()  # percent-encodes spaces and unicode

    frontmatter: dict[str, Any] = {
        "type": c.concept_type,
        "title": c.title,
        "description": c.description,
        "resource": resource,
        "tags": c.tags,
        "status": c.status,
        "entities": c.entities,
        "sensitivity": c.sensitivity,
        "source_hash": c.source_hash,
        "ingest_run": c.ingest_run,
        "generated": {"by": f"okf-kb/{c.model}", "at": c.generated_at},
        # Spec v0.2 §13.1 supersedes `timestamp` with `generated.at`. Both are
        # written: the brief asks for `timestamp` by name, extra keys are
        # explicitly permitted (§4.1), and v0.2 consumers may fall back to it.
        "timestamp": c.generated_at,
        "sources": [
            {
                "id": "source-file",
                "resource": resource,
                "title": source.name,
                "author": "human:local",
                "last_modified": _mtime_date(c.mtime),
            }
        ],
    }

    body: list[str] = []
    if c.description:
        body.append("# Summary\n")
        body.append(f"{c.description}[^source-file]\n")

    body.append("# Source\n")
    body.append(f"| Field | Value |")
    body.append(f"| --- | --- |")
    body.append(f"| Path | `{c.source_path}` |")
    body.append(f"| Format | `{c.ext or 'none'}` |")
    body.append(f"| Size | {c.size:,} bytes |")
    body.append(f"| Extracted words | {c.word_count:,} |")
    body.append(f"| Last modified | {_mtime_date(c.mtime)} |")
    body.append("")

    if c.entities:
        body.append("# Entities\n")
        grouped: dict[str, list[str]] = defaultdict(list)
        for e in c.entities:
            grouped[e["kind"]].append(e["name"])
        for kind in ("person", "organization", "system", "project"):
            if grouped.get(kind):
                names = ", ".join(sorted(set(grouped[kind])))
                body.append(f"- **{kind.capitalize()}**: {names}")
        body.append("")

    if related:
        body.append("# Related\n")
        for peer_id, score in related:
            peer = by_id[peer_id]
            shared = _shared_terms(c, peer)
            why = f" - shares {shared}" if shared else ""
            body.append(f"* [{peer.title}]({link_target(peer_id)}){why}")
        body.append("")

    body.append(f"[^source-file]: {source.name}")

    return Document(frontmatter=frontmatter, body="\n".join(body))


def _shared_terms(a: Concept, b: Concept, limit: int = 3) -> str:
    a_keys = {k: (kind, k) for kind, k, _w in a.terms()}
    shared = []
    for kind, key, _w in b.terms():
        if key in a_keys:
            shared.append(key.split(":", 1)[-1] if kind == "entity" else key)
    return ", ".join(sorted(set(shared))[:limit])


def _mtime_date(mtime: float) -> str:
    try:
        return datetime.fromtimestamp(mtime, tz=timezone.utc).date().isoformat()
    except (ValueError, OSError, OverflowError):
        return date.today().isoformat()


# --- index files -------------------------------------------------------------


def write_indexes(cfg: Config, concepts: list[Concept]) -> int:
    """One index.md per directory, plus the bundle root (§8)."""
    bundle = cfg.bundle_dir
    by_dir: dict[str, list[Concept]] = defaultdict(list)
    for c in concepts:
        by_dir[c.directory].append(c)

    written = 0
    for directory, members in sorted(by_dir.items()):
        if not directory:
            continue
        entries = [
            IndexEntry(
                title=c.title,
                href=f"{c.concept_id.rsplit('/', 1)[-1]}.md",
                description=c.description,
            )
            for c in sorted(members, key=lambda c: c.title.casefold())
        ]
        heading = members[0].concept_type
        write_atomic(
            bundle / directory / "index.md",
            render_index({f"{heading} ({len(entries)})": entries}),
        )
        written += 1

    root_sections: dict[str, list[IndexEntry]] = {}
    dirs = sorted(d for d in by_dir if d)
    root_sections["Concept types"] = [
        IndexEntry(
            title=by_dir[d][0].concept_type,
            href=f"{d}/",
            description=f"{len(by_dir[d])} concepts",
        )
        for d in dirs
    ]
    loose = sorted(by_dir.get("", []), key=lambda c: c.title.casefold())
    if loose:
        root_sections["Concepts at the bundle root"] = [
            IndexEntry(c.title, f"{c.concept_id}.md", c.description) for c in loose
        ]
    write_atomic(
        bundle / "index.md", render_index(root_sections, okf_version=OKF_VERSION)
    )
    return written + 1


# --- the write pass ----------------------------------------------------------


def find_orphans(bundle_dir: Path, live_ids: set[str]) -> list[Path]:
    """Concept files in the bundle that no live record claims any more.

    They appear when a source is deleted, when a root is pruned from the
    config, or when a rename leaves the old ID behind. Reserved filenames are
    never orphans, and a deprecated concept is not one either: §5.4 keeps
    deprecated concepts in place for links and history.
    """
    if not bundle_dir.is_dir():
        return []
    orphans: list[Path] = []
    for path in sorted(bundle_dir.rglob("*.md")):
        # RESERVED, not a hardcoded pair. This used to list index.md and log.md
        # literally, so `entity-review.md` became an orphan the moment `kb
        # graph` started writing it, and `kb bundle --prune` would have deleted
        # the duplicate report on its next run.
        if path.name in RESERVED:
            continue
        if concept_id_for(path, bundle_dir) in live_ids:
            continue
        # A deprecated concept is not an orphan. Spec §5.4 keeps it in place for
        # links and history, and Phase 7 promises the concept file is never
        # deleted. The docstring already said this; nothing enforced it, so
        # prune would have deleted exactly the files the design protects.
        doc = parse(path.read_text(encoding="utf-8", errors="replace"))
        if doc and str(doc.frontmatter.get("status") or "").strip() == "deprecated":
            continue
        orphans.append(path)
    return orphans


def prune_orphans(bundle_dir: Path, orphans: list[Path]) -> int:
    """Delete orphan concepts and any index.md left behind in an empty tree."""
    for path in orphans:
        path.unlink(missing_ok=True)
    removed = len(orphans)

    for directory in sorted(
        (p for p in bundle_dir.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        remaining = [p for p in directory.iterdir() if p.name != "index.md"]
        if not remaining:
            (directory / "index.md").unlink(missing_ok=True)
            directory.rmdir()
    return removed


def write_bundle(
    cfg: Config, mf: Manifest, show_progress: bool = True, prune: bool = False
) -> tuple[dict[str, Any], ValidationReport]:
    """Render every enriched concept, then validate what was written.

    Orphans are reported but not deleted unless `prune` is set. A concept file
    holds real model output that cost real time to produce, so removing one is
    an explicit act rather than a side effect of a rebuild.
    """
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn

    concepts = load_concepts(mf)
    if not concepts:
        return {"concepts": 0, "note": "nothing enriched yet"}, validate_bundle(
            cfg.bundle_dir
        )
    live_ids = {c.concept_id for c in concepts}

    by_id = {c.concept_id: c for c in concepts}
    links = related_map(cfg, concepts)
    bundle = cfg.bundle_dir
    bundle.mkdir(parents=True, exist_ok=True)

    written = 0
    progress = Progress(
        TextColumn("[bold blue]bundle"),
        BarColumn(),
        MofNCompleteColumn(),
        disable=not show_progress,
    )
    with progress:
        task = progress.add_task("bundle", total=len(concepts))
        for c in concepts:
            doc = build_document(cfg, c, links.get(c.concept_id, []), by_id)
            write_atomic(bundle / f"{c.concept_id}.md", render(doc))
            mf.mark_written(c.source_hash)
            written += 1
            progress.update(task, advance=1)
    mf.commit()

    orphans = find_orphans(bundle, live_ids)
    pruned = 0
    if orphans:
        if prune:
            pruned = prune_orphans(bundle, orphans)
            log.info("pruned %d orphaned concept files", pruned)
        else:
            log.warning(
                "%d orphaned concept files remain in the bundle. "
                "Run `kb bundle --prune` to remove them. First: %s",
                len(orphans),
                ", ".join(p.name for p in orphans[:3]),
            )

    indexes = write_indexes(cfg, concepts)

    by_type: dict[str, int] = defaultdict(int)
    by_sensitivity: dict[str, int] = defaultdict(int)
    for c in concepts:
        by_type[c.concept_type] += 1
        by_sensitivity[c.sensitivity] += 1

    total_links = sum(len(v) for v in links.values())
    stats = {
        "concepts": written,
        "indexes": indexes,
        "orphans_found": len(orphans),
        "orphans_pruned": pruned,
        "cross_links": total_links,
        "concepts_with_links": len(links),
        "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        "by_sensitivity": dict(by_sensitivity),
    }

    prepend_log_entry(
        bundle / "log.md",
        date.today(),
        [
            f"* **Update**: wrote {written} concepts across {indexes} directories, "
            f"{total_links} cross-links. Model `{concepts[0].model}`."
        ],
        heading="Bundle Update Log",
    )

    report = validate_bundle(bundle)
    stats["validation"] = {
        "ok": report.ok,
        "errors": len(report.errors),
        "warnings": len(report.warnings),
        "broken_links": report.broken_links,
    }
    return stats, report


# --- deprecation (Phase 7) ---------------------------------------------------


def deprecate_missing(cfg: Config, mf: Manifest) -> dict:
    """Mark concepts whose source file is gone as deprecated.

    Spec §5.4 defines `deprecated` for exactly this: the concept stays in the
    bundle so inbound links and history survive, and consumers know it is no
    longer current. The file is never deleted. `read_bundle` already excludes
    deprecated concepts from both indexes.
    """
    rows = mf.conn.execute(
        """
        SELECT c.source_hash, c.concept_id
        FROM concepts c
        WHERE c.enrich_status = 'ok' AND c.concept_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM files f
              WHERE f.hash = c.source_hash AND f.scan_status = 'included'
          )
        """
    ).fetchall()

    changed: list[str] = []
    for row in rows:
        path = cfg.bundle_dir / f"{row['concept_id']}.md"
        if not path.is_file():
            continue
        doc = parse(path.read_text(encoding="utf-8", errors="replace"))
        if doc is None or doc.frontmatter.get("status") == "deprecated":
            continue
        doc.frontmatter["status"] = "deprecated"
        doc.frontmatter["deprecated_at"] = utc_now_iso()
        write_atomic(path, render(doc))
        changed.append(row["concept_id"])

    if changed:
        log.info("deprecated %d concepts whose source file is gone", len(changed))
        prepend_log_entry(
            cfg.bundle_dir / "log.md",
            date.today(),
            [f"* **Deprecation**: {cid} (source file no longer present)"
             for cid in sorted(changed)[:50]],
            heading="Bundle Update Log",
        )
    return {"deprecated": len(changed), "concept_ids": changed}


def undeprecate(cfg: Config, concept_ids: list[str]) -> int:
    """Bring concepts back when their source file reappears.

    A file that is moved out and back, or restored from Trash, should not stay
    marked dead.
    """
    revived = 0
    for cid in concept_ids:
        path = cfg.bundle_dir / f"{cid}.md"
        if not path.is_file():
            continue
        doc = parse(path.read_text(encoding="utf-8", errors="replace"))
        if doc is None or doc.frontmatter.get("status") != "deprecated":
            continue
        doc.frontmatter["status"] = "stable"
        doc.frontmatter.pop("deprecated_at", None)
        write_atomic(path, render(doc))
        revived += 1
    return revived
