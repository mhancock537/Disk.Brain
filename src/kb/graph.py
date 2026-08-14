"""Property graph over the bundle, in LadybugDB (the maintained Kuzu fork).

The graph is derived, never authoritative. It is dropped and rebuilt from
`bundle/` plus the manifest on every `kb graph` run, which takes seconds, so a
schema change is a rebuild rather than a migration.

Loading goes through Parquet and `COPY ... FROM`, not row-by-row `CREATE`.
Measured at 5,000 nodes in 0.04s against 4,000 relationships in 0.02s.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config, get_logger
from .enrich import is_file_like
from .okf import LINK_RE, RESERVED, concept_id_for, parse

log = get_logger("graph")

SCHEMA = [
    """
    CREATE NODE TABLE Concept(
        concept_id STRING, type STRING, title STRING, description STRING,
        resource STRING, sensitivity STRING, status STRING,
        source_hash STRING, ingest_run STRING,
        generated_by STRING, generated_at STRING, directory STRING,
        PRIMARY KEY(concept_id))
    """,
    """
    CREATE NODE TABLE File(
        hash STRING, path STRING, ext STRING, size INT64, mtime DOUBLE,
        PRIMARY KEY(hash))
    """,
    # `name` is what the Phase 4 brief specifies. Phase 8C calls the same field
    # `surface_form`; the graph rebuilds in seconds, so that rename is free.
    """
    CREATE NODE TABLE Entity(
        key STRING, name STRING, surface_form STRING, kind STRING,
        sources STRING, first_seen STRING, occurrence_count INT64,
        PRIMARY KEY(key))
    """,
    """
    CREATE NODE TABLE Tag(name STRING, concept_count INT64, PRIMARY KEY(name))
    """,
    "CREATE REL TABLE LINKS_TO(FROM Concept TO Concept)",
    "CREATE REL TABLE DERIVED_FROM(FROM Concept TO File)",
    "CREATE REL TABLE MENTIONS(FROM Concept TO Entity)",
    "CREATE REL TABLE TAGGED_AS(FROM Concept TO Tag)",
    "CREATE REL TABLE CHILD_OF(FROM Concept TO Concept)",
]

# Three queries that work against a freshly built graph. `kb cypher` runs them.
EXAMPLE_QUERIES: dict[str, str] = {
    "who-appears-most": """
        MATCH (c:Concept)-[:MENTIONS]->(e:Entity)
        WHERE e.kind = 'person'
        RETURN e.name AS person, count(c) AS documents
        ORDER BY documents DESC LIMIT 15
    """,
    "documents-sharing-an-entity": """
        MATCH (a:Concept)-[:MENTIONS]->(e:Entity)<-[:MENTIONS]-(b:Concept)
        WHERE a.concept_id < b.concept_id AND e.kind = 'organization'
        RETURN e.name AS org, a.title AS doc_a, b.title AS doc_b
        ORDER BY org LIMIT 20
    """,
    "two-hops-from-a-tag": """
        MATCH (t:Tag)<-[:TAGGED_AS]-(c:Concept)-[:MENTIONS]->(e:Entity)
        RETURN t.name AS tag, count(DISTINCT c) AS docs,
               count(DISTINCT e) AS entities
        ORDER BY docs DESC LIMIT 15
    """,
}


@dataclass
class GraphData:
    concepts: list[dict] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)
    entities: list[dict] = field(default_factory=list)
    tags: list[dict] = field(default_factory=list)
    links_to: list[dict] = field(default_factory=list)
    derived_from: list[dict] = field(default_factory=list)
    mentions: list[dict] = field(default_factory=list)
    tagged_as: list[dict] = field(default_factory=list)
    child_of: list[dict] = field(default_factory=list)
    broken_links: list[tuple[str, str]] = field(default_factory=list)


def entity_key(name: str, kind: str) -> str:
    """Casefolded, so `Acme Corp` and `ACME CORP` are one node.

    Different surface forms stay separate on purpose: Phase 8C records every
    form and reports duplicate candidates rather than merging them.
    """
    return f"{kind}:{' '.join(name.split()).casefold()}"


def _str(value: Any) -> str:
    return "" if value is None else str(value)


# --- Phase 8C: hand-written aliases, applied at build time -------------------


def load_aliases(cfg: Config) -> tuple[dict[str, str], dict[str, str]]:
    """Hand-written entity merges. Returns (alias key -> canonical key,
    canonical key -> canonical spelling).

    The file is written by hand and never by this code. Merging is not a thing
    the pipeline decides: it decides what to *report*. Absent file, no merges.

    The spelling is returned separately because it must come from the file, not
    from whichever concept happened to be read first.
    """
    path = cfg.aliases_path
    if not path.is_file():
        return {}, {}
    import tomllib

    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    out: dict[str, str] = {}
    spelling: dict[str, str] = {}
    for entry in raw.get("alias") or []:
        kind = _str(entry.get("kind")).strip()
        canonical = _str(entry.get("canonical")).strip()
        if not kind or not canonical:
            continue
        target = entity_key(canonical, kind)
        spelling[target] = canonical
        for form in entry.get("forms") or []:
            # Kind-scoped on both sides, so an alias can never pull a project
            # node into an organization one.
            key = entity_key(_str(form).strip(), kind)
            if key != target:
                out[key] = target
    return out, spelling


# --- Phase 8C: duplicate candidates, reported and never merged ---------------

EMAIL_RE = re.compile(r"^([^@\s]+)@([^@\s]+\.[^@\s]+)$")


def _tokens(surface: str) -> set[str]:
    return {t for t in re.split(r"[^\w]+", surface.casefold()) if t}


def duplicate_candidates(
    entities: list[dict], mentions: list[dict]
) -> list[dict[str, Any]]:
    """Pairs that *might* be the same person or thing. Reported, never merged.

    Two signals, both from ROADMAP 8C. One surface form contained in another
    (`Dana` inside `Dana Reyes`), and an email whose local part matches a name
    (`dreyes@` against `Dana Reyes`). Pairs are only ever proposed within a
    single `kind`, because `Acme` the organization and `Acme` the project are
    deliberately separate nodes.

    Co-occurrence does not create a pair, it only annotates one: two surface
    forms named by the same concept are more likely to be one entity.
    """
    together: dict[tuple[str, str], int] = defaultdict(int)
    by_concept: dict[str, set[str]] = defaultdict(set)
    for m in mentions:
        by_concept[m["from"]].add(m["to"])
    for keys in by_concept.values():
        ordered = sorted(keys)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:]:
                together[(a, b)] += 1

    by_kind: dict[str, list[dict]] = defaultdict(list)
    for e in entities:
        by_kind[e["kind"]].append(e)

    out: list[dict[str, Any]] = []
    for kind, group in by_kind.items():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                reason = _pair_reason(a["surface_form"], b["surface_form"])
                if not reason:
                    continue
                first, second = (
                    (a, b)
                    if a["occurrence_count"] >= b["occurrence_count"]
                    else (b, a)
                )
                pair = tuple(sorted((a["key"], b["key"])))
                out.append({
                    "a": first["surface_form"],
                    "b": second["surface_form"],
                    "kind": kind,
                    "reason": reason,
                    "occurrences": first["occurrence_count"]
                    + second["occurrence_count"],
                    "rank": first["occurrence_count"],
                    "co_occurrences": together.get(pair, 0),
                })

    out.sort(key=lambda c: (-c["rank"], -c["occurrences"], c["a"]))
    return out


def _pair_reason(a: str, b: str) -> str:
    """Why these two surface forms might be one entity, or "" for no reason."""
    if a.casefold() == b.casefold():
        return ""
    ma, mb = EMAIL_RE.match(a.strip()), EMAIL_RE.match(b.strip())
    if bool(ma) != bool(mb):
        email, name = (ma, b) if ma else (mb, a)
        local = [c for c in re.split(r"[^\w]+", email.group(1).casefold()) if c]
        parts = _tokens(name)
        # `dreyes` against {dana, reyes}: surname whole, given-name initial.
        # A single-token name has no other token to take an initial from, so
        # only the whole-token match applies. That case crashed the first
        # real-corpus run.
        for chunk in local:
            if chunk in parts:
                return f"email local part `{email.group(1)}` matches the name"
            for p in parts:
                others = sorted(parts - {p})
                if len(p) > 2 and others and chunk == f"{others[0][0]}{p}":
                    return f"email local part `{email.group(1)}` matches the name"
        return ""
    if ma and mb:
        return ""
    ta, tb = _tokens(a), _tokens(b)
    if ta and tb and (ta < tb or tb < ta):
        return "one surface form is contained in the other"
    return ""


ENTITY_REVIEW_HEADER = """\
# Entity review

Surface forms that might be the same person or thing. Every one of these is a
separate node today, which is correct: 8C records every form and reports the
overlaps.

These are **never merged automatically, and never on verbal approval in chat.**
A merge happens only by hand, in `config/entity-aliases.toml`, which is applied
at graph build time so every merge stays reversible with an edit and a rebuild.

Sorted by occurrence count, most-used form first.
"""


def write_entity_review(cfg: Config, data: GraphData) -> Path:
    """Write `bundle/entity-review.md`. Returns the path written."""
    cands = duplicate_candidates(data.entities, data.mentions)
    lines = [ENTITY_REVIEW_HEADER, ""]
    if not cands:
        lines.append("No duplicate candidates found.")
    else:
        lines.append(f"{len(cands)} candidate pairs.")
        lines.append("")
        lines.append("| kind | more used | less used | uses | seen together | why |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for c in cands:
            lines.append(
                f"| {c['kind']} | `{c['a']}` | `{c['b']}` | {c['occurrences']} "
                f"| {c['co_occurrences']} | {c['reason']} |"
            )
    path = cfg.bundle_dir / "entity-review.md"
    path.write_text("\n".join(lines) + "\n")
    return path


def collect(cfg: Config) -> GraphData:
    """Read the bundle and the manifest into flat node and edge tables."""
    data = GraphData()
    bundle = cfg.bundle_dir
    if not bundle.is_dir():
        raise RuntimeError(f"no bundle at {bundle}. Run `kb bundle` first.")

    docs: dict[str, dict] = {}
    bodies: dict[str, str] = {}

    for path in sorted(bundle.rglob("*.md")):
        # RESERVED, not a hardcoded pair. Fourth site of the same assumption:
        # this one warned "skipping unparseable concept: entity-review.md" on
        # every graph rebuild, about a file this very module had just written.
        if path.name in RESERVED:
            continue
        doc = parse(path.read_text(encoding="utf-8", errors="replace"))
        if doc is None or not doc.type:
            log.warning("skipping unparseable concept: %s", path.name)
            continue
        cid = concept_id_for(path, bundle)
        docs[cid] = doc.frontmatter
        bodies[cid] = doc.body

    # --- Concept nodes ---
    for cid, fm in docs.items():
        generated = fm.get("generated") or {}
        data.concepts.append(
            {
                "concept_id": cid,
                "type": _str(fm.get("type")),
                "title": _str(fm.get("title")),
                "description": _str(fm.get("description")),
                "resource": _str(fm.get("resource")),
                "sensitivity": _str(fm.get("sensitivity")) or "unknown",
                "status": _str(fm.get("status")) or "stable",
                "source_hash": _str(fm.get("source_hash")),
                "ingest_run": _str(fm.get("ingest_run")),
                "generated_by": _str(generated.get("by") if isinstance(generated, dict) else ""),
                "generated_at": _str(generated.get("at") if isinstance(generated, dict) else ""),
                "directory": cid.rsplit("/", 1)[0] if "/" in cid else "",
            }
        )

    # --- File nodes, from the manifest ---
    hashes = {c["source_hash"] for c in data.concepts if c["source_hash"]}
    if hashes and cfg.manifest_path.is_file():
        conn = sqlite3.connect(cfg.manifest_path)
        conn.row_factory = sqlite3.Row
        seen: set[str] = set()
        for row in conn.execute(
            "SELECT hash, MIN(path) AS path, ext, size, mtime FROM files "
            "WHERE hash IS NOT NULL GROUP BY hash"
        ):
            if row["hash"] in hashes and row["hash"] not in seen:
                seen.add(row["hash"])
                data.files.append(
                    {
                        "hash": row["hash"],
                        "path": row["path"],
                        "ext": row["ext"] or "",
                        "size": int(row["size"] or 0),
                        "mtime": float(row["mtime"] or 0.0),
                    }
                )
        conn.close()
        known = {f["hash"] for f in data.files}
        for c in data.concepts:
            if c["source_hash"] in known:
                data.derived_from.append(
                    {"from": c["concept_id"], "to": c["source_hash"]}
                )

    # --- Entity and Tag nodes ---
    ent_count: dict[str, int] = defaultdict(int)
    ent_meta: dict[str, dict] = {}
    ent_sources: dict[str, set[str]] = defaultdict(set)
    aliases, alias_spelling = load_aliases(cfg)
    tag_count: dict[str, int] = defaultdict(int)

    for cid, fm in docs.items():
        generated = fm.get("generated") or {}
        stamp = _str(generated.get("at") if isinstance(generated, dict) else "")

        for e in fm.get("entities") or []:
            if not isinstance(e, dict):
                continue
            name, kind = _str(e.get("name")).strip(), _str(e.get("kind")).strip()
            if not name or not kind:
                continue
            # Same predicate the enricher uses, so the two cannot disagree
            # about what an entity is. Applied here as well because 2,430
            # concepts already carry filenames in their frontmatter and
            # re-enriching them is a twelve-hour GPU run, while the graph
            # rebuilds in seconds.
            if is_file_like(name):
                continue
            key = entity_key(name, kind)
            if key in aliases:
                # A hand-written merge. The alias file is the only thing that
                # can do this, and deleting the line reverses it on the next
                # rebuild.
                key = aliases[key]
            # The canonical spelling comes from the alias file when there is
            # one, never from whichever concept was read first.
            name = alias_spelling.get(key, name)
            ent_count[key] += 1
            meta = ent_meta.setdefault(
                key,
                # `surface_form` is the 8C name for the display string; `name`
                # is kept so the Phase 4 queries and `kb cypher` keep working.
                {"key": key, "name": name, "surface_form": name, "kind": kind,
                 "first_seen": stamp},
            )
            if stamp and (not meta["first_seen"] or stamp < meta["first_seen"]):
                meta["first_seen"] = stamp
            # Which origins name this entity. Everything is `local` until a
            # cloud source lands, so this is a set of one in practice today.
            ent_sources[key].add(_str(fm.get("source") or "local"))
            data.mentions.append({"from": cid, "to": key})

        for tag in fm.get("tags") or []:
            name = " ".join(_str(tag).split()).casefold()
            if not name:
                continue
            tag_count[name] += 1
            data.tagged_as.append({"from": cid, "to": name})

    for key, meta in ent_meta.items():
        data.entities.append({
            **meta,
            "sources": ",".join(sorted(ent_sources[key])),
            "occurrence_count": ent_count[key],
        })
    for name, count in tag_count.items():
        data.tags.append({"name": name, "concept_count": count})

    # --- LINKS_TO, from markdown cross-links in the body ---
    for cid, body in bodies.items():
        for _label, target in LINK_RE.findall(body):
            if target.startswith(("http://", "https://", "mailto:", "file://", "#")):
                continue
            clean = target.split("#", 1)[0].split("?", 1)[0]
            if not clean.endswith(".md"):
                continue
            if clean.startswith("/"):
                peer = clean.lstrip("/")[:-3]
            else:
                peer = (
                    (bundle / cid).parent.joinpath(clean).resolve()
                    .relative_to(bundle.resolve())
                    .with_suffix("")
                    .as_posix()
                )
            if peer in docs:
                data.links_to.append({"from": cid, "to": peer})
            else:
                # §6.1: a broken link is not malformed, it may be knowledge
                # that has not been written yet. Log it, never fail.
                data.broken_links.append((cid, target))

    # --- CHILD_OF, from the bundle's own directory nesting (§6.1) ---
    for cid in docs:
        parent_dir = cid.rsplit("/", 1)[0] if "/" in cid else ""
        while parent_dir:
            candidate = parent_dir
            if candidate in docs:
                data.child_of.append({"from": cid, "to": candidate})
                break
            parent_dir = parent_dir.rsplit("/", 1)[0] if "/" in parent_dir else ""

    return data


# --- loading -----------------------------------------------------------------


def _write_parquet(rows: list[dict], path: Path, schema) -> Path:
    import pyarrow as pa
    import pyarrow.parquet as pq

    if rows:
        table = pa.Table.from_pylist(rows, schema=schema)
    else:
        table = schema.empty_table()
    pq.write_table(table, path)
    return path


def _schemas():
    import pyarrow as pa

    S = pa.string()
    return {
        "Concept": pa.schema(
            [(n, S) for n in (
                "concept_id", "type", "title", "description", "resource",
                "sensitivity", "status", "source_hash", "ingest_run",
                "generated_by", "generated_at", "directory")]
        ),
        "File": pa.schema([("hash", S), ("path", S), ("ext", S),
                           ("size", pa.int64()), ("mtime", pa.float64())]),
        "Entity": pa.schema([("key", S), ("name", S), ("surface_form", S),
                             ("kind", S), ("sources", S),
                             ("first_seen", S), ("occurrence_count", pa.int64())]),
        "Tag": pa.schema([("name", S), ("concept_count", pa.int64())]),
        "REL": pa.schema([("from", S), ("to", S)]),
    }


def build_graph(cfg: Config) -> dict:
    """Drop and rebuild the graph. Returns node and edge counts."""
    import ladybug

    t0 = time.monotonic()
    data = collect(cfg)

    # 8C: the duplicate report is a build artefact, refreshed every rebuild so
    # it can never go stale against the graph it describes.
    review = write_entity_review(cfg, data)
    log.info("entity review written to %s", review)

    _remove_graph(cfg)
    cfg.graph_dir.parent.mkdir(parents=True, exist_ok=True)

    staging = cfg.data_dir / "graph_staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    schemas = _schemas()
    db = con = None
    try:
        db = ladybug.Database(str(cfg.graph_dir))
        con = ladybug.Connection(db)
        for stmt in SCHEMA:
            con.execute(stmt)

        node_sets = [
            ("Concept", data.concepts, schemas["Concept"]),
            ("File", data.files, schemas["File"]),
            ("Entity", data.entities, schemas["Entity"]),
            ("Tag", data.tags, schemas["Tag"]),
        ]
        rel_sets = [
            ("LINKS_TO", data.links_to),
            ("DERIVED_FROM", data.derived_from),
            ("MENTIONS", data.mentions),
            ("TAGGED_AS", data.tagged_as),
            ("CHILD_OF", data.child_of),
        ]

        for name, rows, schema in node_sets:
            if not rows:
                continue
            p = _write_parquet(rows, staging / f"{name}.parquet", schema)
            con.execute(f'COPY {name} FROM "{p}"')

        for name, rows in rel_sets:
            if not rows:
                continue
            # Duplicate edges add nothing and would inflate every count.
            unique = list({(r["from"], r["to"]): r for r in rows}.values())
            p = _write_parquet(unique, staging / f"{name}.parquet", schemas["REL"])
            con.execute(f'COPY {name} FROM "{p}"')

        counts = {
            "nodes": {
                "Concept": _count(con, "MATCH (n:Concept) RETURN count(n)"),
                "File": _count(con, "MATCH (n:File) RETURN count(n)"),
                "Entity": _count(con, "MATCH (n:Entity) RETURN count(n)"),
                "Tag": _count(con, "MATCH (n:Tag) RETURN count(n)"),
            },
            "edges": {
                "LINKS_TO": _count(con, "MATCH ()-[r:LINKS_TO]->() RETURN count(r)"),
                "DERIVED_FROM": _count(con, "MATCH ()-[r:DERIVED_FROM]->() RETURN count(r)"),
                "MENTIONS": _count(con, "MATCH ()-[r:MENTIONS]->() RETURN count(r)"),
                "TAGGED_AS": _count(con, "MATCH ()-[r:TAGGED_AS]->() RETURN count(r)"),
                "CHILD_OF": _count(con, "MATCH ()-[r:CHILD_OF]->() RETURN count(r)"),
            },
        }
    finally:
        # The handles must be released before anything else opens the store,
        # and before a later rebuild tries to delete it.
        if con is not None:
            con.close()
        if db is not None:
            db.close()
        shutil.rmtree(staging, ignore_errors=True)

    counts["broken_links"] = len(data.broken_links)
    counts["broken_link_examples"] = data.broken_links[:5]
    counts["seconds"] = round(time.monotonic() - t0, 2)
    counts["path"] = str(cfg.graph_dir)
    if data.broken_links:
        log.warning(
            "%d broken cross-links, kept as warnings per OKF 6.1",
            len(data.broken_links),
        )
    return counts


def _remove_graph(cfg: Config) -> None:
    """Delete the store and its sidecars.

    LadybugDB writes a single file, not a directory, plus `.wal` and `.tmp`
    siblings. Both shapes are handled so a version change cannot strand a
    stale store that the next build would silently reuse.
    """
    target = cfg.graph_dir
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
    elif target.exists():
        target.unlink(missing_ok=True)
    for sidecar in target.parent.glob(f"{target.name}.*"):
        if sidecar.is_dir():
            shutil.rmtree(sidecar, ignore_errors=True)
        else:
            sidecar.unlink(missing_ok=True)


def _count(con, query: str) -> int:
    return int(con.execute(query).get_as_df().iloc[0, 0])


def query(cfg: Config, cypher: str):
    """Run one Cypher statement against the built graph."""
    import ladybug

    if not cfg.graph_dir.exists():
        raise FileNotFoundError(
            f"no graph at {cfg.graph_dir}. Run `kb graph` first."
        )
    db = ladybug.Database(str(cfg.graph_dir), read_only=True)
    con = ladybug.Connection(db)
    try:
        return con.execute(cypher).get_as_df()
    finally:
        con.close()
        db.close()
