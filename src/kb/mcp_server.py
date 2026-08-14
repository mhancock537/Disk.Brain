"""MCP server over stdio.

Three tools: `search_knowledge`, `get_concept`, `traverse`.

stdout carries JSON-RPC and nothing else. Every log line in this package goes to
stderr (see `config.setup_logging`), nothing here prints, and the rich console
used by the CLI is deliberately absent from this module. Third-party loggers are
quietened for the same reason: `httpx` and `huggingface_hub` are chatty, and one
stray line on stdout corrupts the protocol stream.

Model loading is lazy. The reranker takes about 14 seconds to load, so it loads
on the first search rather than at startup and is cached for the life of the
process. A long-lived server pays that once.

The three implementations live at module level so they can be tested directly,
without standing up a protocol session.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from .config import Config, get_logger, load_config, setup_logging
from .okf import parse

log = get_logger("mcp")

# Traversals that pass through an intermediate node. A concept does not link to
# another concept via MENTIONS; it shares an Entity with it. Phase 8 asks for
# exactly this: follow MENTIONS from an entity back to the concepts that name it.
VIA_NODE = {
    "MENTIONS": ("Entity", "name"),
    "TAGGED_AS": ("Tag", "name"),
}
DIRECT_EDGES = {"LINKS_TO", "CHILD_OF"}
EDGE_TYPES = sorted(DIRECT_EDGES | set(VIA_NODE))

MAX_LIMIT = 50
MAX_TRAVERSE = 200
NOISY_LOGGERS = ("httpx", "httpcore", "huggingface_hub", "urllib3", "filelock")


def _escape(value: str) -> str:
    return value.replace("'", "''")


def _clean_id(concept_id: str) -> str:
    return concept_id.strip().removesuffix(".md").strip("/")


def _found_by(hit) -> list[str]:
    out = []
    if hit.vector_rank:
        out.append(f"vector#{hit.vector_rank}")
    if hit.bm25_rank:
        out.append(f"bm25#{hit.bm25_rank}")
    if hit.from_graph:
        out.append("graph")
    return out


# --- tool implementations ----------------------------------------------------


def _impl_search(
    cfg: Config,
    query: str,
    limit: int = 10,
    sensitivity_filter: str | None = None,
    source_filter: str | None = None,
    concept_type: str | None = None,
) -> dict[str, Any]:
    from .retrieve import search

    limit = max(1, min(int(limit), MAX_LIMIT))

    # The same guard the CLI and the web endpoint have. Third entry point to
    # one feature, third time this was missing. Without it an empty query
    # embeds an empty string through Ollama and then spends reranker GPU on the
    # result, which is several seconds to answer a question nobody asked.
    if not query.strip():
        return {"query": query, "count": 0, "took_ms": 0.0, "results": []}

    try:
        hits, timings = search(
            cfg,
            query,
            limit=limit,
            sensitivity_filter=sensitivity_filter,
            source_filter=source_filter,
            concept_type_filter=concept_type,
        )
    except FileNotFoundError as exc:
        return {"error": str(exc), "results": [], "hint": "Run `kb index` first."}
    except Exception as exc:
        # A tool must answer, not crash. Ollama being down or the embedding
        # model not being pulled are the realistic causes, and both are
        # actionable if the message says so.
        log.warning("search failed for %r (%s: %s)", query, type(exc).__name__, exc)
        return {
            "error": f"{type(exc).__name__}: {exc}",
            "results": [],
            "hint": (
                "Check that Ollama is running and the embedding model is pulled: "
                "`ollama list`, then `kb doctor`."
            ),
        }

    return {
        "query": query,
        "count": len(hits),
        "took_ms": timings.get("total_ms"),
        "results": [
            {
                "rank": i,
                "concept_id": h.concept_id,
                "title": h.title,
                "heading": h.heading_path,
                "concept_type": h.concept_type,
                "sensitivity": h.sensitivity,
                "source": h.source,
                "score": round(h.score, 4),
                "found_by": _found_by(h),
                "file_path": h.file_path,
                "text": h.text,
            }
            for i, h in enumerate(hits, start=1)
        ],
    }


def _impl_get_concept(cfg: Config, concept_id: str) -> dict[str, Any]:
    clean = _clean_id(concept_id)
    # The ID becomes a filesystem path, so it must not escape the bundle.
    path = (cfg.bundle_dir / f"{clean}.md").resolve()
    try:
        path.relative_to(cfg.bundle_dir.resolve())
    except ValueError:
        return {"error": f"concept_id escapes the bundle: {concept_id!r}"}
    if not path.is_file():
        return {
            "error": f"no concept {clean!r}",
            "hint": "Search first for a valid concept_id.",
        }

    text = path.read_text(encoding="utf-8", errors="replace")
    doc = parse(text)
    return {
        "concept_id": clean,
        "frontmatter": doc.frontmatter if doc else {},
        "markdown": text,
    }


def _impl_traverse(
    cfg: Config,
    concept_id: str,
    hops: int = 1,
    edge_types: list[str] | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    from .graph import query as graph_query

    wanted = [str(e).upper() for e in (edge_types or ["LINKS_TO", "MENTIONS"])]
    unknown = [e for e in wanted if e not in EDGE_TYPES]
    if unknown:
        return {"error": f"unknown edge types {unknown}", "known": EDGE_TYPES}

    hops = max(1, min(int(hops), 2))
    limit = max(1, min(int(limit), MAX_TRAVERSE))
    start = _escape(_clean_id(concept_id))
    found: list[dict[str, Any]] = []

    for edge in wanted:
        if edge in DIRECT_EDGES:
            cypher = (
                f"MATCH (a:Concept)-[:{edge}*1..{hops}]-(b:Concept) "
                f"WHERE a.concept_id = '{start}' AND b.concept_id <> '{start}' "
                f"RETURN DISTINCT b.concept_id AS concept_id, b.title AS title, "
                f"b.type AS concept_type, b.sensitivity AS sensitivity, "
                f"'' AS via LIMIT {limit}"
            )
        else:
            label, prop = VIA_NODE[edge]
            cypher = (
                f"MATCH (a:Concept)-[:{edge}]->(n:{label})<-[:{edge}]-(b:Concept) "
                f"WHERE a.concept_id = '{start}' AND b.concept_id <> '{start}' "
                f"RETURN DISTINCT b.concept_id AS concept_id, b.title AS title, "
                f"b.type AS concept_type, b.sensitivity AS sensitivity, "
                f"n.{prop} AS via LIMIT {limit}"
            )
        try:
            df = graph_query(cfg, cypher)
        except FileNotFoundError as exc:
            return {"error": str(exc), "hint": "Run `kb graph` first.", "neighbours": []}
        except Exception as exc:
            log.warning("traverse failed for %s via %s (%s)", start, edge, exc)
            continue

        for _, row in df.iterrows():
            found.append(
                {
                    "concept_id": row["concept_id"],
                    "title": row["title"],
                    "concept_type": row["concept_type"],
                    "sensitivity": row["sensitivity"],
                    "edge": edge,
                    "via": row["via"] or None,
                }
            )

    # One neighbour reached by two edge types is one neighbour with two reasons.
    merged: dict[str, dict[str, Any]] = {}
    for n in found:
        entry = merged.setdefault(
            n["concept_id"],
            {
                **{
                    k: n[k]
                    for k in ("concept_id", "title", "concept_type", "sensitivity")
                },
                "reasons": [],
            },
        )
        entry["reasons"].append({"edge": n["edge"], "via": n["via"]})

    out = sorted(merged.values(), key=lambda e: (-len(e["reasons"]), e["concept_id"]))
    return {
        "concept_id": start,
        "edge_types": wanted,
        "hops": hops,
        "count": len(out),
        "neighbours": out[:limit],
    }


# --- server ------------------------------------------------------------------


def build_server(cfg: Config):
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(
        name="okf-kb",
        title="Local knowledge base",
        version="0.1.0",
        instructions=(
            "Semantic search over local documents. The knowledge layer is an OKF "
            "bundle: every result belongs to a concept, and every concept derives "
            "from a real file on this machine. Use search_knowledge to find "
            "passages, get_concept to read a whole concept, and traverse to walk "
            "from one concept to related ones."
        ),
    )

    @server.tool(
        title="Search the knowledge base",
        description=(
            "Hybrid search over the local document corpus: vector similarity, "
            "BM25 keyword matching, a one-hop graph expansion, and a reranker. "
            "Returns passages with the concept and source file each came from."
        ),
    )
    def search_knowledge(
        query: str,
        limit: int = 10,
        sensitivity_filter: Literal["work", "personal", "unknown"] | None = None,
        source_filter: (
            Literal["local", "gdrive", "onedrive", "fireflies", "granola"] | None
        ) = None,
        concept_type: str | None = None,
    ) -> dict[str, Any]:
        """Find passages that answer a question.

        Args:
            query: What to look for, in natural language.
            limit: How many passages to return. 1 to 50.
            sensitivity_filter: Restrict to work, personal, or unknown material.
            source_filter: Restrict to one origin. Everything is `local` today.
            concept_type: Restrict to one document type, e.g. Runbook or Contract.
        """
        return _impl_search(
            cfg, query, limit, sensitivity_filter, source_filter, concept_type
        )

    @server.tool(
        title="Read a whole concept",
        description=(
            "Return the complete markdown of one OKF concept, frontmatter and "
            "body. Use the concept_id from a search result."
        ),
    )
    def get_concept(concept_id: str) -> dict[str, Any]:
        """Read one concept in full.

        Args:
            concept_id: The concept's ID, for example `runbook/restart-the-widget`.
        """
        return _impl_get_concept(cfg, concept_id)

    @server.tool(
        title="Walk the knowledge graph",
        description=(
            "From one concept, return related concepts. LINKS_TO follows explicit "
            "cross-links, MENTIONS returns concepts naming the same entity, and "
            "TAGGED_AS returns concepts sharing a tag."
        ),
    )
    def traverse(
        concept_id: str,
        hops: int = 1,
        edge_types: list[str] | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Find concepts connected to this one.

        Args:
            concept_id: Where to start.
            hops: How far to walk along direct edges. 1 or 2.
            edge_types: Any of LINKS_TO, MENTIONS, TAGGED_AS, CHILD_OF.
            limit: Maximum neighbours to return.
        """
        return _impl_traverse(cfg, concept_id, hops, edge_types, limit)

    return server


def main(config_path: Path | None = None) -> None:
    """Entry point for `kb serve` and for the MCP client config blocks."""
    cfg = load_config(config_path)
    # Logging to stderr is what keeps stdout clean enough for JSON-RPC.
    setup_logging(cfg.log_level, cfg.log_format)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    log.info("okf-kb MCP server starting, bundle=%s", cfg.bundle_dir)
    build_server(cfg).run("stdio")


if __name__ == "__main__":
    main()
