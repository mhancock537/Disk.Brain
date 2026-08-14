"""Local-LLM enrichment: one catalogue record per source document.

The model runs through Ollama on this machine. Thinking is disabled and the
response is constrained by a JSON schema, so the reply is always a parseable
object and never carries a reasoning block.

Resume state lives in the `concepts` table, keyed by source hash, never
inferred from which files exist in the bundle. A half-written file on disk
looks complete; the database does not lie.
"""

from __future__ import annotations

import fnmatch
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config, EnrichConfig, get_logger
from .manifest import Manifest, utcnow

log = get_logger("enrich")

ENTITY_KINDS = ("person", "organization", "system", "project")

SYSTEM_PROMPT = (
    "You catalogue documents for a personal knowledge base. "
    "Return only the JSON object described by the schema. "
    "Every field must be grounded in the text you are given. "
    "Never invent a fact, a name, or a date that is not present. "
    "If the text is too thin to judge, say so plainly in the description."
)


def response_schema(cfg: EnrichConfig) -> dict[str, Any]:
    """Constrained decoding removes a whole class of parse-and-retry logic."""
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "concept_type": {"type": "string", "enum": cfg.type_names},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": cfg.max_tags,
            },
            "entities": {
                "type": "array",
                "maxItems": cfg.max_entities,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "kind": {"type": "string", "enum": list(ENTITY_KINDS)},
                    },
                    "required": ["name", "kind"],
                },
            },
            "sensitivity": {"type": "string", "enum": ["work", "personal"]},
        },
        "required": [
            "title",
            "description",
            "concept_type",
            "tags",
            "entities",
            "sensitivity",
        ],
    }


# --- Entities are things, not files ------------------------------------------

# Extensions no technology is ever named after. `.js` is deliberately absent:
# Next.js and Node.js are real entities and app.js is a file, so that case is
# decided on the stem instead.
FILE_EXT_RE = re.compile(
    r"\.(md|markdown|py|ts|tsx|json|ya?ml|toml|sh|bash|zsh|txt|csv|tsv"
    r"|pdf|docx?|xlsx?|pptx?|log|ini|cfg|conf|env|lock|sql|rs|go|rb|java)$",
    re.I,
)
JS_EXT_RE = re.compile(r"\.(jsx?|mjs|cjs)$", re.I)
URL_RE = re.compile(r"^(https?://|www\.)", re.I)


def is_file_like(name: str | None) -> bool:
    """Whether an extracted "entity" is really a filename, path or URL.

    The model returns them because they are prominent nouns in technical
    documents. They then become Entity nodes with MENTIONS edges, so
    "documents that mention README.md" links every repository in the corpus to
    every other, and the duplicate-candidate report fills with them.

    The rule has to discriminate, not just pattern-match. `Next.js`, `Node.js`
    and `Vue.js` match every naive filename check and are real; `BSA/AML` and
    `MSP/Datto` match every naive path check and are real. Measured on the live
    corpus: this drops 351 of 16,045 mentions (2.2%) and keeps all of those.
    """
    n = (name or "").strip()
    if not n:
        return False
    if URL_RE.match(n):
        return True
    if FILE_EXT_RE.search(n):
        return True
    if JS_EXT_RE.search(n):
        # A technology stem is a single capitalised word (Next, Node, Vue).
        # A filename stem is lowercase, hyphenated or a path.
        stem = JS_EXT_RE.sub("", n)
        return not (stem[:1].isupper() and stem.isalnum())
    return "/" in n and FILE_EXT_RE.search(n.split("/")[-1]) is not None


def build_prompt(cfg: EnrichConfig, filename: str, source_path: str, text: str) -> str:
    excerpt = " ".join(text.split()[: cfg.prompt_words])
    type_menu = "\n".join(f"- {name}: {desc}" for name, desc in cfg.types.items())
    return (
        f"Filename: {filename}\n"
        f"Source path: {source_path}\n\n"
        f"--- first {cfg.prompt_words} words of the document ---\n"
        f"{excerpt}\n"
        f"--- end of excerpt ---\n\n"
        "Produce a catalogue record.\n\n"
        f"concept_type must be exactly one of:\n{type_menu}\n\n"
        "Judge the type from what the document says, not from its file extension.\n"
        f"title: a specific human title, at most 80 characters, in Title Case.\n"
        "description: one sentence saying what this document is and what it covers.\n"
        f"tags: up to {cfg.max_tags} short lowercase topic tags, no punctuation.\n"
        "entities: the people, organizations, systems and projects the document "
        "actually names. Use the name as written. Omit generic words. "
        "Never list filenames, file paths or URLs: README.md and "
        "src/app.py are not entities, though Next.js and PostgreSQL are.\n"
        "sensitivity: work if it concerns a business, product, customer or "
        "employer; personal otherwise."
    )


@dataclass
class Record:
    title: str
    description: str
    concept_type: str
    tags: list[str]
    entities: list[dict[str, str]]
    sensitivity: str


def _clean(raw: dict[str, Any], cfg: EnrichConfig, fallback_title: str) -> Record:
    """Normalise the model's output. The schema guarantees shape, not hygiene."""
    title = str(raw.get("title") or "").strip() or fallback_title
    title = " ".join(title.split())[:120]

    description = " ".join(str(raw.get("description") or "").split())[:400]

    ctype = str(raw.get("concept_type") or "").strip()
    if ctype not in cfg.types:
        ctype = "Other" if "Other" in cfg.types else next(iter(cfg.types))

    seen: set[str] = set()
    tags: list[str] = []
    for t in raw.get("tags") or []:
        tag = " ".join(str(t).lower().split()).strip(" -_/")
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    tags = tags[: cfg.max_tags]

    ents: list[dict[str, str]] = []
    ent_seen: set[tuple[str, str]] = set()
    for e in raw.get("entities") or []:
        if not isinstance(e, dict):
            continue
        name = " ".join(str(e.get("name") or "").split())
        kind = str(e.get("kind") or "").lower()
        if not name or kind not in ENTITY_KINDS:
            continue
        if is_file_like(name):
            # Prompted against as well, but a prompt is a request and this is
            # the guarantee. Filenames are the single most common thing the
            # model mistakes for an entity in technical documents.
            continue
        key = (name.casefold(), kind)
        if key in ent_seen:
            continue
        ent_seen.add(key)
        ents.append({"name": name, "kind": kind})
    ents = ents[: cfg.max_entities]

    sensitivity = str(raw.get("sensitivity") or "").lower()
    if sensitivity not in ("work", "personal"):
        sensitivity = cfg.sensitivity_default

    return Record(title, description, ctype, tags, ents, sensitivity)


def sensitivity_from_path(cfg: EnrichConfig, source_path: str) -> str | None:
    """Path rules from config.toml, applied before the model is consulted."""
    for glob in cfg.personal_globs:
        if fnmatch.fnmatch(source_path, glob):
            return "personal"
    for glob in cfg.work_globs:
        if fnmatch.fnmatch(source_path, glob):
            return "work"
    return None


def enrich_one(cfg: Config, filename: str, source_path: str, text: str) -> Record:
    """One Ollama call. Raises on transport failure so the caller can retry."""
    import ollama

    ec = cfg.enrich
    response = ollama.chat(
        model=ec.model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_prompt(ec, filename, source_path, text)},
        ],
        think=False,
        format=response_schema(ec),
        options={
            "temperature": ec.temperature,
            "num_ctx": ec.num_ctx,
        },
        keep_alive="30m",
    )
    raw = json.loads(response["message"]["content"])
    record = _clean(raw, ec, fallback_title=Path(filename).stem)

    forced = sensitivity_from_path(ec, source_path)
    if forced:
        record.sensitivity = forced
    return record


def check_model(cfg: Config) -> tuple[bool, str]:
    """Confirm the configured model is actually pulled before a long run."""
    import ollama

    try:
        names = {m.model for m in ollama.list().models}
    except Exception as exc:
        return False, f"Ollama not reachable: {exc}"
    if cfg.enrich.model in names:
        return True, cfg.enrich.model
    return False, (
        f"model {cfg.enrich.model!r} is not pulled. Available: {sorted(names) or 'none'}. "
        f"Run: ollama pull {cfg.enrich.model}"
    )


def run_enrich(
    cfg: Config,
    mf: Manifest,
    limit: int | None = None,
    show_progress: bool = True,
    recent_first: bool = False,
    max_seconds: float | None = None,
) -> dict[str, Any]:
    """Enrich every pending document. Safe to interrupt and rerun.

    `max_seconds` is a wall-clock budget checked between documents, so a run
    always stops on a clean boundary with its work committed. Used by the duty
    cycle to bound how long the GPU is held.
    """
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )

    from .okf import allocate_concept_id

    ok, detail = check_model(cfg)
    if not ok:
        raise RuntimeError(detail)

    todo = mf.enrichable(recent_first=recent_first)
    if limit:
        todo = todo[:limit]

    run_id = mf.start_run("enrich")
    taken = mf.taken_concept_ids()
    stats = {"ok": 0, "failed": 0, "seconds": 0.0, "model": cfg.enrich.model}
    latencies: list[float] = []
    t0 = time.monotonic()

    progress = Progress(
        TextColumn("[bold blue]enrich"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("{task.fields[name]}"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        disable=not show_progress,
    )

    with progress:
        task = progress.add_task("enrich", total=len(todo), name="")
        for row in todo:
            if max_seconds is not None and time.monotonic() - t0 >= max_seconds:
                stats["stopped_on_budget"] = True
                log.info("wall-clock budget of %.0fs reached, stopping cleanly",
                         max_seconds)
                break
            src = Path(row["path"])
            progress.update(task, advance=1, name=src.name[:40])

            extract_path = row["extract_path"]
            if not extract_path or not Path(extract_path).exists():
                _fail(mf, row["hash"], "extracted text missing", run_id, cfg)
                stats["failed"] += 1
                continue

            text = Path(extract_path).read_text(encoding="utf-8", errors="replace")
            record = None
            last_error = ""
            for attempt in range(1, cfg.enrich.max_attempts + 1):
                started = time.monotonic()
                try:
                    record = enrich_one(cfg, src.name, str(src), text)
                    latencies.append(time.monotonic() - started)
                    break
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    log.warning(
                        "enrich attempt %d/%d failed for %s (%s)",
                        attempt,
                        cfg.enrich.max_attempts,
                        src.name,
                        last_error,
                    )

            if record is None:
                _fail(mf, row["hash"], last_error[:500], run_id, cfg)
                stats["failed"] += 1
                mf.commit()
                continue

            concept_id = row["concept_id"] or allocate_concept_id(
                record.concept_type, record.title, row["hash"], taken
            )
            taken.add(concept_id)

            mf.save_concept(
                {
                    "source_hash": row["hash"],
                    "concept_id": concept_id,
                    "concept_type": record.concept_type,
                    "title": record.title,
                    "description": record.description,
                    "tags": json.dumps(record.tags),
                    "entities": json.dumps(record.entities),
                    "sensitivity": record.sensitivity,
                    "status": "stable",
                    "enrich_status": "ok",
                    "error": None,
                    "model": cfg.enrich.model,
                    "generated_at": utcnow(),
                    "ingest_run": run_id,
                }
            )
            stats["ok"] += 1
            mf.commit()  # every document, so an interrupt loses at most one

    stats["seconds"] = round(time.monotonic() - t0, 1)
    if latencies:
        stats["mean_latency"] = round(sum(latencies) / len(latencies), 1)
    stats["run_id"] = run_id
    mf.finish_run(run_id, json.dumps(stats))
    return stats


def _fail(mf: Manifest, source_hash: str, error: str, run_id: str, cfg: Config) -> None:
    mf.save_concept(
        {
            "source_hash": source_hash,
            "concept_id": mf.existing_concept_id(source_hash),
            "concept_type": None,
            "title": None,
            "description": None,
            "tags": None,
            "entities": None,
            "sensitivity": None,
            "status": "stable",
            "enrich_status": "failed",
            "error": error,
            "model": cfg.enrich.model,
            "generated_at": utcnow(),
            "ingest_run": run_id,
        }
    )
