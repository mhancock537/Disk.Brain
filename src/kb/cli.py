"""kb command line.

Phase 1 surface: scan, extract, report, doctor.
Logs go to stderr; report tables go to stdout.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from .config import Config, load_config, setup_logging
from .manifest import Manifest

app = typer.Typer(add_completion=False, help="Local OKF knowledge base over your documents.")
console = Console()


def _load(config: Optional[Path], verbose: bool) -> Config:
    """Load config, or fail with one line instead of a traceback.

    Every command in this file goes through here, so a typo in `--config` used
    to render 35 lines of traceback through cli.py for all 20 of them. A wrong
    path and a malformed file are both user mistakes, not crashes.
    """
    try:
        cfg = load_config(config)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except tomllib.TOMLDecodeError as exc:
        target = config or "config.toml"
        console.print(f"[red]{target} is not valid TOML[/red]: {exc}")
        raise typer.Exit(1) from exc
    setup_logging("DEBUG" if verbose else cfg.log_level, cfg.log_format)
    return cfg


@app.command()
def scan(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    no_hash: bool = typer.Option(
        False, "--no-hash", help="Stat pass only. Review the denylist before hashing."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Walk the configured roots and record every file in the manifest."""
    cfg = _load(config, verbose)
    from .manifest import scan as do_scan

    with Manifest(cfg.manifest_path) as mf:
        stats = do_scan(cfg, mf, do_hash=not no_hash)
        console.print_json(json.dumps(stats))
        from .report import scan_report

        scan_report(cfg, mf, console)


@app.command()
def extract(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="Stop after N documents."),
    ext: Optional[str] = typer.Option(None, "--ext", help="Only this extension, e.g. pdf."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Extract plain markdown for every hashed file still pending."""
    cfg = _load(config, verbose)
    from .extract.runner import run_extract

    with Manifest(cfg.manifest_path) as mf:
        stats = run_extract(cfg, mf, limit=limit, only_ext=ext)
        console.print_json(json.dumps(stats))
        from .report import extract_report

        extract_report(cfg, mf, console)


@app.command()
def report(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    failures: int = typer.Option(20, "--failures", help="Failure groups to show."),
) -> None:
    """Print the Phase 1 checkpoint report from the existing manifest."""
    cfg = _load(config, False)
    with Manifest(cfg.manifest_path) as mf:
        from .report import extract_report, scan_report

        scan_report(cfg, mf, console)
        extract_report(cfg, mf, console, top_failures=failures)


@app.command()
def enrich(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="Stop after N documents."),
    max_seconds: Optional[float] = typer.Option(
        None, "--max-seconds", help="Wall-clock budget. Stops between documents."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run the local LLM over extracted text to build catalogue records."""
    cfg = _load(config, verbose)
    from .enrich import run_enrich

    with Manifest(cfg.manifest_path) as mf:
        stats = run_enrich(cfg, mf, limit=limit, max_seconds=max_seconds)
        console.print_json(json.dumps(stats))


@app.command()
def bundle(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    prune: bool = typer.Option(
        False, "--prune", help="Delete concept files no live record claims."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Write the OKF bundle from enriched records, then validate it."""
    cfg = _load(config, verbose)
    from .bundle import write_bundle
    from .report import validation_report

    with Manifest(cfg.manifest_path) as mf:
        stats, report = write_bundle(cfg, mf, prune=prune)
        console.print_json(json.dumps(stats))
        validation_report(report, console)
        if not report.ok:
            raise typer.Exit(1)


@app.command()
def validate(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    show: int = typer.Option(15, "--show", help="Findings to print per level."),
) -> None:
    """Check the bundle against the OKF v0.2 conformance rules."""
    cfg = _load(config, False)
    from .okf import validate_bundle
    from .report import validation_report

    report = validate_bundle(cfg.bundle_dir)
    validation_report(report, console, show=show)
    if not report.ok:
        raise typer.Exit(1)


@app.command(name="index")
def index_cmd(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Rebuild the vector store and the BM25 index from bundle/."""
    cfg = _load(config, verbose)
    from .index import run_index
    from .report import index_report

    stats = run_index(cfg)
    index_report(stats, console)


@app.command(name="embed-bench")
def embed_bench(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    models: str = typer.Option(
        "qwen3-embedding:0.6b,qwen3-embedding:4b", "--models", help="Comma separated."
    ),
    chunks: int = typer.Option(500, "--chunks"),
    probes: int = typer.Option(150, "--probes"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Compare embedding models on held-out sentence probes from this corpus."""
    cfg = _load(config, verbose)
    from .bench import run_benchmark
    from .report import benchmark_report

    results, meta = run_benchmark(
        cfg, [m.strip() for m in models.split(",") if m.strip()], chunks, probes
    )
    benchmark_report(results, meta, console)


@app.command(name="graph")
def graph_cmd(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Drop and rebuild the property graph from bundle/ plus the manifest."""
    cfg = _load(config, verbose)
    from .graph import build_graph
    from .report import graph_report

    counts = build_graph(cfg)
    graph_report(counts, console)


@app.command()
def cypher(
    query_or_name: str = typer.Argument(
        ..., help="A Cypher statement, or the name of a built-in example."
    ),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    list_examples: bool = typer.Option(False, "--list", help="Show the examples."),
) -> None:
    """Run a Cypher query against the graph. `--list` shows worked examples."""
    cfg = _load(config, False)
    from .graph import EXAMPLE_QUERIES, query as run_query

    if list_examples:
        for name, q in EXAMPLE_QUERIES.items():
            console.print(f"[bold cyan]{name}[/bold cyan]")
            console.print(f"[dim]{q.strip()}[/dim]\n")
        return

    statement = EXAMPLE_QUERIES.get(query_or_name, query_or_name)
    try:
        df = run_query(cfg, statement)
    except Exception as exc:
        # A typo in a query is a user mistake, not a crash. The graph engine
        # raises a bare RuntimeError, which typer renders as a full traceback
        # through the database internals: forty lines that answer nothing.
        console.print(f"[red]query failed[/red]: {exc}")
        console.print("[dim]`kb cypher --list` shows worked examples.[/dim]")
        raise typer.Exit(1) from exc

    from .report import dataframe_report

    dataframe_report(df, console, title=query_or_name[:60])


@app.command()
def search(
    query: str = typer.Argument(..., help="What to look for."),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    limit: int = typer.Option(15, "--limit", "-n"),
    sensitivity: Optional[str] = typer.Option(None, "--sensitivity", help="work | personal | unknown"),
    source: Optional[str] = typer.Option(None, "--source", help="local | gdrive | onedrive | fireflies | granola"),
    concept_type: Optional[str] = typer.Option(None, "--type", help="e.g. Runbook"),
    full: bool = typer.Option(False, "--full", help="Print whole chunks, not snippets."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Hybrid search: vector + BM25 + graph hop + reranker."""
    cfg = _load(config, verbose)
    from .retrieve import search as run_search
    from .report import search_report

    # The same guard the web endpoint has. Without it `kb search ""` ran the
    # whole pipeline, embedding an empty string through Ollama and then
    # spending 2.8 seconds of reranker GPU on the result. Two entry points to
    # one feature disagreeing about a basic case is the sort of thing that
    # stays broken because each looks fine on its own.
    if not query.strip():
        console.print("[dim]nothing to search for[/dim]")
        return

    hits, timings = run_search(
        cfg, query, limit=limit, sensitivity_filter=sensitivity,
        source_filter=source, concept_type_filter=concept_type,
    )
    search_report(query, hits, timings, console, full=full)


@app.command(name="eval")
def eval_cmd(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    questions: Optional[Path] = typer.Option(None, "--questions", help="Path to a TOML eval set."),
    limit: int = typer.Option(15, "--limit", "-n"),
    ablation: bool = typer.Option(False, "--ablation", help="Also run with stages disabled."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Score retrieval against eval/questions.toml."""
    cfg = _load(config, verbose)
    from .evaluate import run_ablation, run_eval
    from .report import ablation_report, eval_report

    stats = run_eval(cfg, questions, limit=limit)
    eval_report(stats, console)
    if ablation:
        ablation_report(run_ablation(cfg, questions, limit=limit), console)


@app.command()
def samples(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    count: int = typer.Option(10, "--count", "-n"),
) -> None:
    """Print whole concept files, spread across types, for review."""
    cfg = _load(config, False)
    from .report import print_samples

    with Manifest(cfg.manifest_path) as mf:
        print_samples(cfg, mf, console, count=count)


@app.command()
def watch(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    seconds: Optional[float] = typer.Option(
        None, "--seconds", help="Stop after N seconds. For demos and tests."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Watch for file changes. Cheap work runs inline, the rest queues."""
    cfg = _load(config, verbose)
    from .watch import run_watch

    counters = run_watch(cfg, once_seconds=seconds)
    console.print_json(json.dumps(counters))


@app.command()
def drain(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="Override the cap."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run the queued expensive work: enrich, rewrite, reindex, rebuild."""
    cfg = _load(config, verbose)
    from .watch import run_drain

    stats = run_drain(cfg, limit=limit)
    console.print_json(json.dumps(stats, default=str))


@app.command()
def serve(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
) -> None:
    """Run the MCP server on stdio. stdout carries JSON-RPC only."""
    from .mcp_server import main as serve_main

    serve_main(config)


@app.command()
def granola(
    export: Path = typer.Argument(..., help="Meeting export JSON from the Granola connector."),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Import Granola meetings as markdown the normal pipeline can read.

    Writes to `[granola] notes_dir`. Add that directory to `[[scan.roots]]` and
    `kb scan` picks the meetings up like any other document, so nothing about
    extraction, enrichment, bundling or indexing has to know what a meeting is.

    Idempotent: re-importing the same export changes nothing on disk, so an
    unchanged transcript never re-enters the enrichment queue.
    """
    cfg = _load(config, verbose)
    from .granola import import_meetings

    try:
        counts = import_meetings(cfg, export)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    console.print_json(json.dumps(counts, default=str))
    if counts["total_in_export"] == 0:
        console.print(
            "[yellow]That export held no meetings.[/yellow] The Granola account "
            "returned none on 2026-08-10: its tier serves only the last 30 days "
            "and there were none in that window."
        )
    elif counts["imported"] or counts["updated"]:
        console.print(
            f"Run [bold]kb scan[/bold] then [bold]kb drain[/bold] to pull them in, "
            f"once {counts['notes_dir']} is a root in config.toml."
        )


@app.command()
def web(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    port: int = typer.Option(8765, "--port", "-p", help="Loopback port."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Serve the local search page. Ctrl-C to stop."""
    cfg = _load(config, verbose)
    from .web import make_server

    try:
        httpd = make_server(cfg, port=port)
    except OSError as exc:
        console.print(f"[red]cannot bind port {port}[/red]: {exc}")
        raise typer.Exit(1) from exc

    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    console.print(f"[bold]okf-kb[/bold] on {url}   (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        console.print("\nstopped")
    finally:
        httpd.server_close()


@app.command(name="mcp-config")
def mcp_config(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
) -> None:
    """Print the exact JSON to paste into Claude Code and the desktop app."""
    cfg = _load(config, False)
    from .report import mcp_config_report

    mcp_config_report(cfg, console)


@app.command()
def doctor(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
) -> None:
    """Check configured roots, external binaries, and OCR before a long run."""
    cfg = _load(config, False)
    import shutil

    console.print(f"[bold]config:[/bold] {cfg.root_dir / 'config.toml'}")
    console.print(f"[bold]manifest:[/bold] {cfg.manifest_path}")

    for root in cfg.roots:
        mark = "[green]ok[/green]"
        if not root.enabled:
            mark = "[dim]disabled[/dim]"
        elif not root.path.is_dir():
            mark = "[red]missing[/red]"
        else:
            try:
                next(iter(root.path.iterdir()), None)
            except PermissionError:
                mark = "[red]permission denied[/red]"
        console.print(f"  root {root.path}  {mark}")

    for binary in ("pandoc", "textutil"):
        found = shutil.which(binary)
        console.print(
            f"  binary {binary}: "
            + (f"[green]{found}[/green]" if found else "[yellow]not found[/yellow]")
        )

    try:
        from ocrmac import ocrmac  # noqa: F401

        console.print("  ocrmac: [green]importable[/green]")
    except Exception as exc:
        console.print(f"  ocrmac: [red]{exc}[/red]")

    try:
        import pymupdf

        console.print(f"  pymupdf: [green]{pymupdf.__version__}[/green]")
    except Exception as exc:
        console.print(f"  pymupdf: [red]{exc}[/red]")

    from .enrich import check_model

    ok, detail = check_model(cfg)
    console.print(
        f"  enrich model: "
        + (f"[green]{detail}[/green]" if ok else f"[red]{detail}[/red]")
    )


if __name__ == "__main__":
    app()
