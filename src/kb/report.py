"""Checkpoint reporting over the manifest."""

from __future__ import annotations

import re
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .config import Config
from .manifest import Manifest
from .okf import ValidationReport


def _mb(n: int | None) -> str:
    return f"{(n or 0) / 1_048_576:,.1f}"


def error_class(err: str | None) -> str:
    """Collapse a raw error string into a group label.

    Paths, page numbers and byte offsets are stripped so that fifty variants of
    the same fault collapse into one row.
    """
    if not err:
        return "unknown"
    first = err.split(" | ")[0]
    first = re.sub(r"/[^\s'\"]*", "<path>", first)
    # Paths containing spaces fragment into several <path> tokens: rejoin them
    # so "/Users/a b/c.pdf" collapses to one placeholder, not three.
    first = re.sub(r"(?:\S*<path>\S*)(?:\s+\S*<path>\S*)*", "<path>", first)
    first = re.sub(r"\d+", "N", first)
    return first[:80]


def scan_report(cfg: Config, mf: Manifest, console: Console, top_ext: int = 25) -> None:
    c = mf.conn

    status = c.execute(
        "SELECT scan_status, COUNT(*) n, SUM(size) bytes FROM files "
        "GROUP BY scan_status ORDER BY n DESC"
    ).fetchall()
    t = Table(title="Scan status", header_style="bold")
    t.add_column("status")
    t.add_column("files", justify="right")
    t.add_column("MB", justify="right")
    for r in status:
        t.add_row(r["scan_status"], f"{r['n']:,}", _mb(r["bytes"]))
    console.print(t)

    ext = c.execute(
        """
        SELECT CASE WHEN ext = '' THEN '(none)' ELSE ext END AS ext,
               COUNT(*) n, SUM(size) bytes
        FROM files WHERE scan_status = 'included'
        GROUP BY ext ORDER BY n DESC LIMIT ?
        """,
        (top_ext,),
    ).fetchall()
    t = Table(title=f"Included files by extension (top {top_ext})", header_style="bold")
    t.add_column("ext")
    t.add_column("files", justify="right")
    t.add_column("MB", justify="right")
    for r in ext:
        t.add_row(r["ext"], f"{r['n']:,}", _mb(r["bytes"]))
    console.print(t)

    roots = c.execute(
        "SELECT root, COUNT(*) n, SUM(size) bytes FROM files "
        "WHERE scan_status = 'included' GROUP BY root ORDER BY n DESC"
    ).fetchall()
    t = Table(title="Included files by root", header_style="bold")
    t.add_column("root")
    t.add_column("files", justify="right")
    t.add_column("MB", justify="right")
    for r in roots:
        t.add_row(str(r["root"]), f"{r['n']:,}", _mb(r["bytes"]))
    console.print(t)

    dedup = c.execute(
        "SELECT COUNT(*) total, COUNT(DISTINCT hash) uniq FROM files "
        "WHERE scan_status = 'included' AND hash IS NOT NULL"
    ).fetchone()
    if dedup["total"]:
        dupes = dedup["total"] - dedup["uniq"]
        console.print(
            f"\n[bold]Hashed:[/bold] {dedup['total']:,} files, "
            f"{dedup['uniq']:,} unique, {dupes:,} duplicates "
            f"({dupes / dedup['total'] * 100:.1f}%)"
        )


def extract_report(cfg: Config, mf: Manifest, console: Console, top_failures: int = 20) -> None:
    c = mf.conn

    # Group over distinct hashes first: summing word_count across raw rows
    # would count every duplicate copy of a document again.
    rows = c.execute(
        """
        SELECT extract_status, COUNT(*) docs, SUM(word_count) words
        FROM (
            SELECT DISTINCT hash, extract_status, word_count
            FROM files WHERE scan_status = 'included' AND hash IS NOT NULL
        )
        GROUP BY extract_status ORDER BY docs DESC
        """
    ).fetchall()
    t = Table(title="Extraction status (by unique document)", header_style="bold")
    t.add_column("status")
    t.add_column("docs", justify="right")
    t.add_column("words", justify="right")
    total = attempted = ok = 0
    for r in rows:
        t.add_row(r["extract_status"], f"{r['docs']:,}", f"{r['words'] or 0:,}")
        total += r["docs"]
        if r["extract_status"] != "pending":
            attempted += r["docs"]
        if r["extract_status"] == "ok":
            ok += r["docs"]
    console.print(t)

    no_route = c.execute(
        "SELECT COUNT(*) n FROM files WHERE scan_status = 'no_route'"
    ).fetchone()["n"]

    if attempted:
        console.print(
            f"[bold]Success rate:[/bold] {ok:,}/{attempted:,} = "
            f"{ok / attempted * 100:.1f}% of routable documents"
            + (f"   ([dim]{total - attempted:,} still pending[/dim])" if total > attempted else "")
        )
        if no_route:
            console.print(
                f"[dim]Not counted above: {no_route:,} files had no extractor "
                f"route and were never attempted.[/dim]"
            )

    words = c.execute(
        "SELECT SUM(word_count) w FROM (SELECT DISTINCT hash, word_count FROM files "
        "WHERE extract_status = 'ok')"
    ).fetchone()["w"]
    console.print(f"[bold]Total extracted words:[/bold] {words or 0:,}")

    eng = c.execute(
        """
        SELECT extract_engine, COUNT(DISTINCT hash) docs, SUM(word_count) words
        FROM files WHERE extract_status = 'ok'
        GROUP BY extract_engine ORDER BY docs DESC
        """
    ).fetchall()
    if eng:
        t = Table(title="Engines used", header_style="bold")
        t.add_column("engine")
        t.add_column("docs", justify="right")
        t.add_column("words", justify="right")
        for r in eng:
            t.add_row(r["extract_engine"] or "-", f"{r['docs']:,}", f"{r['words'] or 0:,}")
        console.print(t)

    # `empty` rows carry no error string: the extractor ran and found nothing.
    # They still belong here, or the report claims a clean run while documents
    # silently produced no text.
    fails = c.execute(
        """
        SELECT ext, extract_status, error, COUNT(DISTINCT hash) docs, MIN(path) example
        FROM files
        WHERE extract_status IN ('failed', 'empty')
        GROUP BY ext, extract_status, error
        """
    ).fetchall()

    grouped: dict[tuple[str, str], dict] = {}
    for r in fails:
        cls = error_class(r["error"]) if r["error"] else (
            "no text extracted" if r["extract_status"] == "empty" else "unknown"
        )
        key = (r["ext"] or "(none)", cls)
        g = grouped.setdefault(key, {"docs": 0, "example": r["example"]})
        g["docs"] += r["docs"]

    if grouped:
        t = Table(
            title=f"Top {top_failures} failure groups (extension x error class)",
            header_style="bold",
        )
        t.add_column("#", justify="right")
        t.add_column("ext")
        t.add_column("docs", justify="right")
        t.add_column("error class")
        t.add_column("example")
        for i, ((e, cls), g) in enumerate(
            sorted(grouped.items(), key=lambda kv: -kv[1]["docs"])[:top_failures], 1
        ):
            t.add_row(str(i), e, f"{g['docs']:,}", cls, Path(g["example"]).name[:38])
        console.print(t)
    else:
        console.print("[green]No extraction failures recorded.[/green]")

    if cfg.extract_out_dir.exists():
        size = sum(
            f.stat().st_size for f in cfg.extract_out_dir.rglob("*.md") if f.is_file()
        )
        console.print(f"[bold]data/extracted:[/bold] {_mb(size)} MB on disk")


# --- Phase 2 reporting -------------------------------------------------------


def validation_report(
    report: ValidationReport, console: Console, show: int = 15
) -> None:
    """Errors fail a run. Warnings never do: the spec tells consumers not to
    reject a bundle over broken links or missing optional keys (§11)."""
    verdict = (
        "[bold green]CONFORMANT[/bold green]"
        if report.ok
        else "[bold red]NOT CONFORMANT[/bold red]"
    )
    console.print(
        f"\nOKF v0.2 validation: {verdict}   "
        f"{report.concepts:,} concepts, {report.indexes:,} index files, "
        f"{report.logs} log files, {report.links:,} internal links"
    )
    console.print(
        f"  errors: {len(report.errors):,}   warnings: {len(report.warnings):,}   "
        f"broken links: {report.broken_links:,} "
        f"[dim](tolerated by spec §6.1)[/dim]"
    )

    for level, colour in (("error", "red"), ("warning", "yellow")):
        findings = report.errors if level == "error" else report.warnings
        if not findings:
            continue
        grouped: dict[str, int] = {}
        example: dict[str, str] = {}
        for f in findings:
            key = re.sub(r"`[^`]*`", "`...`", f.message)
            key = re.sub(r"broken link: .*", "broken link", key)
            grouped[key] = grouped.get(key, 0) + 1
            example.setdefault(key, f.path)
        t = Table(title=f"{level.capitalize()}s", header_style="bold")
        t.add_column("count", justify="right")
        t.add_column("finding")
        t.add_column("example path")
        for msg, n in sorted(grouped.items(), key=lambda kv: -kv[1])[:show]:
            t.add_row(f"{n:,}", msg[:70], example[msg][:46])
        console.print(t, style=colour)


def print_samples(cfg: Config, mf: Manifest, console: Console, count: int = 10) -> None:
    """Whole concept files, spread across types, for human review."""
    rows = mf.conn.execute(
        """
        SELECT concept_id, concept_type FROM concepts
        WHERE enrich_status = 'ok' AND concept_id IS NOT NULL
        ORDER BY concept_type, concept_id
        """
    ).fetchall()
    if not rows:
        console.print("[yellow]No enriched concepts yet.[/yellow]")
        return

    by_type: dict[str, list[str]] = {}
    for r in rows:
        by_type.setdefault(r["concept_type"], []).append(r["concept_id"])

    picked: list[str] = []
    while len(picked) < count and any(by_type.values()):
        for t in list(by_type):
            if by_type[t]:
                picked.append(by_type[t].pop(len(by_type[t]) // 2))
            if len(picked) >= count:
                break

    for i, cid in enumerate(picked[:count], 1):
        path = cfg.bundle_dir / f"{cid}.md"
        console.rule(f"[bold]{i}/{min(count, len(picked))}  {cid}")
        if path.exists():
            console.print(path.read_text(encoding="utf-8"))
        else:
            console.print(f"[yellow]not written yet: {path}[/yellow]")


# --- Phase 3 reporting -------------------------------------------------------


def index_report(stats: dict, console: Console) -> None:
    e = stats["embed"]
    t = Table(title="Index rebuild", header_style="bold")
    t.add_column("metric")
    t.add_column("value", justify="right")
    rows = [
        ("concepts", f"{stats['concepts']:,}"),
        ("concepts with no extracted text", f"{stats['concepts_without_text']:,}"),
        ("chunks", f"{stats['chunks']:,}"),
        ("chunks per concept", f"{stats['chunks_per_concept']}"),
        ("mean tokens per chunk", f"{stats['tokens_mean']}"),
        ("largest chunk (tokens)", f"{stats['tokens_max']:,}"),
        ("total tokens", f"{stats['tokens_total']:,}"),
        ("chunking time", f"{stats['chunk_seconds']}s"),
        ("embedding model", e["model"]),
        ("vector dimensions", f"{e['dimensions']:,}"),
        ("vectors written", f"{e['vectors']:,}"),
        ("embedding time", f"{e['seconds']}s"),
        ("embedding rate", f"{e['chunks_per_second']} chunks/s"),
        ("FTS5 rows", f"{stats['fts_rows']:,}"),
        ("FTS5 build time", f"{stats['fts_seconds']}s"),
        ("total wall clock", f"{stats['total_seconds']}s"),
    ]
    for k, v in rows:
        t.add_row(k, v)
    console.print(t)

    d = Table(title="Disk usage per index", header_style="bold")
    d.add_column("store")
    d.add_column("MB", justify="right")
    for name, mb in stats["disk_mb"].items():
        d.add_row(name, f"{mb:,.1f}")
    console.print(d)

    if stats["by_type"]:
        c = Table(title="Chunks by concept type", header_style="bold")
        c.add_column("type")
        c.add_column("chunks", justify="right")
        for k, v in list(stats["by_type"].items())[:15]:
            c.add_row(k, f"{v:,}")
        console.print(c)

    if stats["by_source"]:
        console.print(f"[bold]By source:[/bold] {stats['by_source']}")
    if stats["problems"]:
        console.print(f"[yellow]{len(stats['problems'])} problems, first few:[/yellow]")
        for p in stats["problems"][:5]:
            console.print(f"  - {p}")


def benchmark_report(results, meta: dict, console: Console) -> None:
    t = Table(
        title=f"Embedding benchmark: {meta['probes']} probes over "
        f"{meta['chunks_indexed']:,} chunks",
        header_style="bold",
    )
    t.add_column("model")
    t.add_column("dims", justify="right")
    t.add_column("recall@1", justify="right")
    t.add_column("recall@5", justify="right")
    t.add_column("recall@10", justify="right")
    t.add_column("MRR", justify="right")
    t.add_column("chunks/s", justify="right")
    t.add_column("embed time", justify="right")
    for r in results:
        t.add_row(
            r.model, f"{r.dimensions:,}",
            f"{r.recall_at_1:.3f}", f"{r.recall_at_5:.3f}", f"{r.recall_at_10:.3f}",
            f"{r.mrr:.3f}", f"{r.chunks_per_second}", f"{r.embed_seconds}s",
        )
    console.print(t)
    console.print(f"[dim]Method: {meta['method']}[/dim]")

    if len(results) >= 2:
        best = max(results, key=lambda r: r.recall_at_5)
        fastest = max(results, key=lambda r: r.chunks_per_second)
        console.print(
            f"\n[bold]Best recall@5:[/bold] {best.model} ({best.recall_at_5:.3f})   "
            f"[bold]Fastest:[/bold] {fastest.model} ({fastest.chunks_per_second} chunks/s)"
        )


# --- Phase 4 reporting -------------------------------------------------------


def graph_report(counts: dict, console: Console) -> None:
    n = Table(title="Graph nodes", header_style="bold")
    n.add_column("label")
    n.add_column("count", justify="right")
    for label, c in counts["nodes"].items():
        n.add_row(label, f"{c:,}")
    console.print(n)

    e = Table(title="Graph edges", header_style="bold")
    e.add_column("relationship")
    e.add_column("count", justify="right")
    for label, c in counts["edges"].items():
        e.add_row(label, f"{c:,}")
    console.print(e)

    console.print(
        f"[bold]Built in[/bold] {counts['seconds']}s at {counts['path']}"
    )
    if counts["broken_links"]:
        console.print(
            f"[yellow]{counts['broken_links']:,} broken cross-links[/yellow] "
            f"[dim](tolerated, OKF §6.1)[/dim]"
        )
        for src, target in counts["broken_link_examples"]:
            console.print(f"  [dim]{src} -> {target}[/dim]")


def dataframe_report(df, console: Console, title: str = "") -> None:
    if df is None or len(df) == 0:
        console.print("[yellow]No rows.[/yellow]")
        return
    t = Table(title=title or None, header_style="bold")
    for col in df.columns:
        t.add_column(str(col))
    for _, row in df.iterrows():
        t.add_row(*[str(v) for v in row.tolist()])
    console.print(t)
    console.print(f"[dim]{len(df)} rows[/dim]")


# --- Phase 5 reporting -------------------------------------------------------


def search_report(query, hits, timings, console: Console, full: bool = False) -> None:
    console.print(f"\n[bold]Query:[/bold] {query}")
    if not hits:
        console.print("[yellow]No results.[/yellow]")
        return

    for i, h in enumerate(hits, 1):
        origin = []
        if h.vector_rank:
            origin.append(f"vec#{h.vector_rank}")
        if h.bm25_rank:
            origin.append(f"bm25#{h.bm25_rank}")
        if h.from_graph:
            origin.append("graph")
        heading = f" > {h.heading_path}" if h.heading_path else ""
        console.print(
            f"\n[bold cyan]{i:2}.[/bold cyan] [bold]{h.title}[/bold]{heading}"
            f"   [dim]{h.concept_type} | {h.sensitivity} | {h.source}[/dim]"
        )
        console.print(
            f"    [green]rerank {h.rerank:.4f}[/green]  "
            f"[dim]rrf {h.rrf:.4f}  {' '.join(origin) or 'graph-only'}[/dim]"
        )
        console.print(f"    [dim]{h.concept_id}[/dim]")
        if h.file_path:
            console.print(f"    [dim]{h.file_path}[/dim]")
        body = h.text if full else h.snippet()
        console.print(f"    {body}")

    console.print(
        f"\n[dim]embed {timings['embed_ms']}ms | vector {timings['vector_ms']}ms | "
        f"bm25 {timings['bm25_ms']}ms | graph {timings['graph_ms']}ms "
        f"({timings['graph_neighbours']} neighbours) | "
        f"rerank {timings['rerank_ms']}ms ({timings['reranked']} candidates) | "
        f"[bold]total {timings['total_ms']}ms[/bold][/dim]"
    )


def eval_report(stats: dict, console: Console) -> None:
    t = Table(title=f"Retrieval eval: {stats['questions']} questions", header_style="bold")
    t.add_column("metric")
    t.add_column("value", justify="right")
    for label, key in (
        ("hit rate @1", "hit_at_1"),
        ("hit rate @3", "hit_at_3"),
        ("hit rate @5", "hit_at_5"),
        ("hit rate @10", "hit_at_10"),
        ("MRR", "mrr"),
    ):
        t.add_row(label, f"{stats[key]:.3f}")
    lat = stats["latency_ms"]
    for label, key in (("median latency", "median"), ("mean latency", "mean"),
                       ("p90 latency", "p90"), ("max latency", "max")):
        t.add_row(label, f"{lat[key]:,.0f} ms")
    console.print(t)

    if stats["misses"]:
        console.print(f"[yellow]{len(stats['misses'])} miss(es):[/yellow]")
        for q in stats["misses"]:
            console.print(f"  - {q}")
    else:
        console.print("[green]Every question found its document.[/green]")


def ablation_report(rows: list[dict], console: Console) -> None:
    t = Table(title="Ablation: what each stage contributes", header_style="bold")
    t.add_column("configuration")
    t.add_column("hit@1", justify="right")
    t.add_column("hit@5", justify="right")
    t.add_column("MRR", justify="right")
    t.add_column("median ms", justify="right")
    for r in rows:
        t.add_row(
            r["label"], f"{r['hit_at_1']:.3f}", f"{r['hit_at_5']:.3f}",
            f"{r['mrr']:.3f}", f"{r['latency_ms']['median']:,.0f}",
        )
    console.print(t)


# --- Phase 6 reporting -------------------------------------------------------


def mcp_config_report(cfg: Config, console: Console) -> None:
    """Print ready-to-paste MCP client configuration."""
    import json
    import sys

    # sys.executable resolves through the venv symlink to the uv-managed
    # interpreter, which does not see the venv's site-packages. The venv's own
    # python is the one that can import kb and its dependencies.
    venv_python = cfg.root_dir / ".venv" / "bin" / "python"
    python = str(venv_python if venv_python.is_file() else Path(sys.executable))
    block = {
        "mcpServers": {
            "okf-kb": {
                "command": python,
                "args": ["-m", "kb.mcp_server"],
                "env": {"PYTHONPATH": str((cfg.root_dir / "src").resolve())},
            }
        }
    }
    pretty = json.dumps(block, indent=2)

    console.print("\n[bold]Claude desktop app[/bold]")
    console.print(
        "[dim]~/Library/Application Support/Claude/claude_desktop_config.json[/dim]"
    )
    console.print(pretty)

    console.print("\n[bold]Claude Code[/bold]")
    console.print("[dim]One command, no file editing:[/dim]")
    console.print(
        f"  claude mcp add okf-kb --scope user -- {python} -m kb.mcp_server"
    )
    console.print(
        f"[dim]  (set PYTHONPATH={(cfg.root_dir / 'src').resolve()} if kb is not installed in that venv)[/dim]"
    )
    console.print("\n[dim]Or paste into ~/.claude.json under mcpServers:[/dim]")
    console.print(json.dumps(block["mcpServers"], indent=2))
