"""Configuration loading and the structured stderr logger.

Logging is set up here, in the lowest-level module, because the Phase 6 MCP
server speaks JSON-RPC on stdout. Nothing in this package may ever print to
stdout except the CLI's own report writers.
"""

from __future__ import annotations

import logging
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_NAME = "config.toml"


def repo_root() -> Path:
    """Directory holding config.toml, found by walking up from this file."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / DEFAULT_CONFIG_NAME).is_file():
            return parent
    # Installed without a config beside it: fall back to cwd.
    return Path.cwd()


@dataclass(frozen=True)
class Root:
    path: Path
    enabled: bool
    sensitivity: str


@dataclass(frozen=True)
class OcrConfig:
    enabled: bool
    chars_per_page_threshold: int
    scanned_page_fraction: float
    sample_pages: int
    max_pages: int
    dpi: int
    images: bool


@dataclass(frozen=True)
class EnrichConfig:
    model: str
    prompt_words: int
    temperature: float
    num_ctx: int
    timeout_seconds: int
    max_tags: int
    max_entities: int
    max_attempts: int
    types: dict[str, str]
    sensitivity_default: str
    work_globs: list[str]
    personal_globs: list[str]

    @property
    def type_names(self) -> list[str]:
        return list(self.types)


@dataclass(frozen=True)
class ChunkConfig:
    max_tokens: int
    overlap_ratio: float


@dataclass(frozen=True)
class EmbedConfig:
    model: str
    batch_size: int
    metric: str
    use_ann: bool


@dataclass(frozen=True)
class RetrieveConfig:
    vector_top_k: int
    bm25_top_k: int
    rrf_k: int
    fuse_top_n: int
    graph_hops: int
    graph_neighbour_chunks: int
    rerank_max_candidates: int
    limit: int
    rerank_enabled: bool
    rerank_model: str
    rerank_instruction: str
    rerank_batch_size: int
    rerank_doc_chars: int
    # Below this reranker score a result set is the corpus's nearest miss
    # rather than an answer. Tunable because the right value depends on the
    # corpus, and because both the web interface and the eval's abstention
    # metric read it: hard-coding it would put the same number in two places
    # and let them drift.
    weak_score: float


@dataclass(frozen=True)
class WatchConfig:
    root: Path
    require_configured_root: bool
    debounce_seconds: float
    max_queue: int


@dataclass(frozen=True)
class DrainConfig:
    max_documents: int
    rebuild_graph: bool


@dataclass(frozen=True)
class BundleConfig:
    max_related: int
    link_rarity_ceiling: int
    min_link_score: float


@dataclass(frozen=True)
class Config:
    root_dir: Path
    roots: list[Root]
    deny_globs: list[str]
    max_file_bytes: int
    follow_symlinks: bool
    same_device_only: bool
    include_source_code: bool
    extract_out_dir: Path
    extract_min_chars: int
    ocr: OcrConfig
    enrich: EnrichConfig
    chunk: ChunkConfig
    embed: EmbedConfig
    retrieve: RetrieveConfig
    watch: WatchConfig
    drain: DrainConfig
    bundle: BundleConfig
    log_level: str
    log_format: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def data_dir(self) -> Path:
        return self.root_dir / "data"

    @property
    def manifest_path(self) -> Path:
        return self.data_dir / "manifest.db"

    @property
    def bundle_dir(self) -> Path:
        return self.root_dir / "bundle"

    @property
    def lance_dir(self) -> Path:
        return self.data_dir / "lance"

    @property
    def fts_path(self) -> Path:
        return self.data_dir / "fts.db"

    @property
    def graph_dir(self) -> Path:
        return self.data_dir / "graph"

    @property
    def granola_dir(self) -> Path:
        """Where imported meetings are written.

        A normal directory on disk, deliberately, so it can be a scan root and
        the existing pipeline handles meetings with no new code. Configurable
        because where notes live is the operator's decision, not this code's.

        A relative path resolves against `root_dir`, and the default IS
        relative. That is not cosmetic: an absolute default like
        `~/granola-notes` means any Config built without a `[granola]` section
        writes into the real home directory. The first test run did exactly
        that and created files in `~/granola-notes` before anything caught it.
        """
        raw = str(self.raw.get("granola", {}).get("notes_dir", "data/granola-notes"))
        expanded = Path(raw).expanduser()
        return expanded if expanded.is_absolute() else self.root_dir / expanded

    @property
    def aliases_path(self) -> Path:
        """Hand-edited entity merges (8C). Applied at graph build, so removing
        a line and rebuilding reverses the merge."""
        return self.root_dir / "config" / "entity-aliases.toml"

    def enabled_roots(self) -> list[Root]:
        return [r for r in self.roots if r.enabled]


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p))).resolve()


def load_config(path: Path | None = None) -> Config:
    root_dir = path.parent.resolve() if path else repo_root()
    cfg_path = path or (root_dir / DEFAULT_CONFIG_NAME)
    if not cfg_path.is_file():
        raise FileNotFoundError(f"no config at {cfg_path}")

    with cfg_path.open("rb") as fh:
        raw = tomllib.load(fh)

    scan = raw.get("scan", {})
    roots = [
        Root(
            path=_expand(r["path"]),
            enabled=bool(r.get("enabled", True)),
            sensitivity=str(r.get("sensitivity", "personal")),
        )
        for r in scan.get("roots", [])
    ]

    extract = raw.get("extract", {})
    ocr_raw = extract.get("ocr", {})
    ocr = OcrConfig(
        enabled=bool(ocr_raw.get("enabled", True)),
        chars_per_page_threshold=int(ocr_raw.get("chars_per_page_threshold", 100)),
        scanned_page_fraction=float(ocr_raw.get("scanned_page_fraction", 0.6)),
        sample_pages=int(ocr_raw.get("sample_pages", 8)),
        max_pages=int(ocr_raw.get("max_pages", 40)),
        dpi=int(ocr_raw.get("dpi", 200)),
        images=bool(ocr_raw.get("images", False)),
    )

    out_dir = Path(extract.get("out_dir", "data/extracted"))
    if not out_dir.is_absolute():
        out_dir = root_dir / out_dir

    en = raw.get("enrich", {})
    sens = en.get("sensitivity", {})
    types = en.get("types", {})
    if not types:
        # A bundle needs at least one type to sort concepts into.
        types = {"Reference": "general reference material", "Other": "none of the above"}
    enrich = EnrichConfig(
        model=str(en.get("model", "qwen3:14b-q4_K_M")),
        prompt_words=int(en.get("prompt_words", 2000)),
        temperature=float(en.get("temperature", 0.2)),
        num_ctx=int(en.get("num_ctx", 8192)),
        timeout_seconds=int(en.get("timeout_seconds", 240)),
        max_tags=int(en.get("max_tags", 6)),
        max_entities=int(en.get("max_entities", 12)),
        max_attempts=int(en.get("max_attempts", 2)),
        types=dict(types),
        sensitivity_default=str(sens.get("default", "personal")),
        work_globs=list(sens.get("work_globs", [])),
        personal_globs=list(sens.get("personal_globs", [])),
    )

    ch = raw.get("chunk", {})
    chunk = ChunkConfig(
        max_tokens=int(ch.get("max_tokens", 800)),
        overlap_ratio=float(ch.get("overlap_ratio", 0.15)),
    )

    em = raw.get("embed", {})
    embed = EmbedConfig(
        model=str(em.get("model", "qwen3-embedding:0.6b")),
        batch_size=int(em.get("batch_size", 32)),
        metric=str(em.get("metric", "cosine")),
        use_ann=bool(em.get("use_ann", False)),
    )

    rt = raw.get("retrieve", {})
    rr = rt.get("rerank", {})
    retrieve = RetrieveConfig(
        vector_top_k=int(rt.get("vector_top_k", 50)),
        bm25_top_k=int(rt.get("bm25_top_k", 50)),
        rrf_k=int(rt.get("rrf_k", 60)),
        fuse_top_n=int(rt.get("fuse_top_n", 20)),
        graph_hops=int(rt.get("graph_hops", 1)),
        graph_neighbour_chunks=int(rt.get("graph_neighbour_chunks", 2)),
        rerank_max_candidates=int(rt.get("rerank_max_candidates", 60)),
        limit=int(rt.get("limit", 15)),
        rerank_enabled=bool(rr.get("enabled", True)),
        rerank_model=str(rr.get("model", "mlx-community/Qwen3-Reranker-0.6B-4bit")),
        rerank_instruction=str(
            rr.get("instruction",
                   "Judge whether the Document meets the requirements based on the Query.")
        ),
        rerank_batch_size=int(rr.get("batch_size", 16)),
        rerank_doc_chars=int(rr.get("doc_chars", 700)),
        weak_score=float(rt.get("weak_score", 0.20)),
    )

    wt = raw.get("watch", {})
    watch = WatchConfig(
        root=_expand(str(wt.get("root", "~/"))),
        require_configured_root=bool(wt.get("require_configured_root", True)),
        debounce_seconds=float(wt.get("debounce_seconds", 3.0)),
        max_queue=int(wt.get("max_queue", 5000)),
    )

    dr = raw.get("drain", {})
    drain = DrainConfig(
        max_documents=int(dr.get("max_documents", 200)),
        rebuild_graph=bool(dr.get("rebuild_graph", True)),
    )

    bn = raw.get("bundle", {})
    bundle = BundleConfig(
        max_related=int(bn.get("max_related", 8)),
        link_rarity_ceiling=int(bn.get("link_rarity_ceiling", 60)),
        min_link_score=float(bn.get("min_link_score", 0.35)),
    )

    log = raw.get("logging", {})

    return Config(
        root_dir=root_dir,
        roots=roots,
        deny_globs=list(scan.get("deny", {}).get("globs", [])),
        max_file_bytes=int(scan.get("max_file_bytes", 100 * 1024 * 1024)),
        follow_symlinks=bool(scan.get("follow_symlinks", False)),
        same_device_only=bool(scan.get("same_device_only", True)),
        include_source_code=bool(scan.get("include_source_code", False)),
        extract_out_dir=out_dir,
        extract_min_chars=int(extract.get("min_chars", 30)),
        ocr=ocr,
        enrich=enrich,
        chunk=chunk,
        embed=embed,
        retrieve=retrieve,
        watch=watch,
        drain=drain,
        bundle=bundle,
        log_level=str(log.get("level", "INFO")).upper(),
        log_format=str(log.get("format", "text")),
        raw=raw,
    )


# --- logging -----------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        import json

        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key.startswith("kb_"):
                payload[key[3:]] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_configured = False


def setup_logging(level: str = "INFO", fmt: str = "text") -> None:
    """Attach a single stderr handler to the `kb` logger. Never touches stdout."""
    global _configured
    logger = logging.getLogger("kb")
    if _configured:
        logger.setLevel(level)
        return

    handler = logging.StreamHandler(stream=sys.stderr)
    if fmt == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        )
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"kb.{name}")
