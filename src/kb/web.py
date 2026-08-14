"""Local web interface. One page, four routes, standard library only.

The point of this surface: you know roughly what a document said but not what
it was called. So it optimises for recognition. Results group by document, rows
carry the folder and the modified date, and the matching passage is the preview.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import Config
from .retrieve import Hit


@dataclass
class Document:
    """One document in a result list, however many passages it matched."""

    concept_id: str
    title: str
    concept_type: str
    sensitivity: str
    source: str
    preview: str
    file_path: str
    concept: dict
    # The best passage's relevance. From the reranker when it ran, which is a
    # calibrated 0-1 judgement, otherwise the fused rank score, which is not
    # comparable across queries and must not be read as confidence.
    score: float = 0.0
    passage_count: int = 0
    headings: list[str] = field(default_factory=list)


def group_hits(hits: list[Hit]) -> list[Document]:
    """Collapse ranked passages into ranked documents.

    Order is first-seen, so a document sits where its best passage ranked. The
    preview is that best passage, never a concatenation.
    """
    by_id: dict[str, Document] = {}
    order: list[Document] = []

    for h in hits:
        doc = by_id.get(h.concept_id)
        if doc is None:
            doc = Document(
                concept_id=h.concept_id,
                title=h.title,
                concept_type=h.concept_type,
                sensitivity=h.sensitivity,
                source=h.source,
                preview=h.snippet(),
                file_path=h.file_path,
                concept=h.concept,
            )
            by_id[h.concept_id] = doc
            order.append(doc)
        doc.passage_count += 1
        doc.score = max(doc.score, float(h.score or 0.0))
        if h.heading_path and h.heading_path not in doc.headings:
            doc.headings.append(h.heading_path)

    return order


def file_meta(cfg: Config, paths: list[str]) -> dict[str, dict]:
    """Folder name and modified time per source path, read from the manifest.

    One query for the whole result page rather than one per row. Opened
    read-only: this surface never writes to the manifest.
    """
    out: dict[str, dict] = {}
    wanted = [p for p in paths if p]
    if not wanted or not cfg.manifest_path.is_file():
        return out

    conn = sqlite3.connect(f"file:{cfg.manifest_path}?mode=ro", uri=True)
    try:
        marks = ",".join("?" * len(wanted))
        for path, mtime in conn.execute(
            f"SELECT path, mtime FROM files WHERE path IN ({marks})", wanted
        ):
            out[path] = {"folder": Path(path).parent.name, "mtime": mtime}
    finally:
        conn.close()
    return out


def resolve_source_path(cfg: Config, concept_id: str) -> str | None:
    """The source file for a concept, or None.

    The only path source for `/api/open`. The client sends a concept_id and
    never a path, so a caller cannot ask this server to open an arbitrary file.
    The id is bound as a query parameter, never interpolated, so `%` and `'`
    are literal text rather than SQL.

    `MIN(path)` matches what graph.py does for File nodes, so deduplicated
    files resolve the same way everywhere.
    """
    if not concept_id or not cfg.manifest_path.is_file():
        return None

    conn = sqlite3.connect(f"file:{cfg.manifest_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT MIN(f.path) FROM concepts c JOIN files f ON f.hash = c.source_hash "
            "WHERE c.concept_id = ?",
            (concept_id,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row and row[0] else None


def build_payload(
    cfg: Config, hits: list[Hit], timings: dict, degraded: str = ""
) -> dict:
    """The exact JSON `/api/search` returns.

    Everything the detail pane needs travels with the result, because `Hit`
    already carries the concept frontmatter and the source path. That saves the
    page a second request per click.
    """
    docs = group_hits(hits)
    meta = file_meta(cfg, [d.file_path for d in docs])

    documents = []
    for d in docs:
        m = meta.get(d.file_path, {})
        documents.append({
            "concept_id": d.concept_id,
            "title": d.title,
            "concept_type": d.concept_type,
            "sensitivity": d.sensitivity,
            "source": d.source,
            "preview": d.preview,
            "passage_count": d.passage_count,
            "score": round(d.score, 4),
            "headings": d.headings,
            "file_path": d.file_path,
            "folder": m.get("folder", ""),
            "mtime": m.get("mtime"),
            "description": str(d.concept.get("description") or ""),
            "tags": d.concept.get("tags") or [],
            "entities": [
                str(e.get("name", "")) if isinstance(e, dict) else str(e)
                for e in (d.concept.get("entities") or [])
            ],
        })

    best = max((d.score for d in docs), default=0.0)

    return {
        "documents": documents,
        "total_documents": len(documents),
        "total_passages": sum(d.passage_count for d in docs),
        "best_score": round(best, 4),
        # Whether the corpus actually holds an answer, as opposed to whether
        # anything came back. Retrieval always returns its nearest neighbours,
        # so without this a query with no answer looks exactly like one with a
        # good answer. Measured on the live corpus 2026-08-10: a query with an
        # answer tops out at 0.989, one without at 0.0032.
        #
        # Only meaningful when the reranker ran. Degraded results carry fused
        # rank scores, which are near zero by construction and would brand
        # every keyword result a bad match for the wrong reason.
        "weak": bool(docs) and not degraded and best < cfg.retrieve.weak_score,
        "timings": timings,
        "degraded": degraded,
    }


def _related(bundle: Path, concept_id: str, body: str) -> list[dict]:
    """Sibling concepts this one links to, from the markdown body.

    These are not in frontmatter, which is why search results cannot carry them
    and `/api/concept` exists. Only links that resolve to a concept file that
    actually exists are returned: the OKF spec tolerates broken links (§6.1) and
    the bundle has some, but a dead link is not a destination worth offering.
    """
    from .okf import LINK_RE, parse

    out: list[dict] = []
    seen: set[str] = set()

    for _label, target in LINK_RE.findall(body or ""):
        if target.startswith(("http://", "https://", "mailto:", "file://", "#")):
            continue
        clean = target.split("#", 1)[0].split("?", 1)[0]
        if not clean.endswith(".md"):
            continue

        if clean.startswith("/"):
            peer = clean.lstrip("/")[:-3]
        else:
            try:
                peer = str(
                    (bundle / concept_id).parent.joinpath(clean).resolve()
                    .relative_to(bundle).with_suffix("")
                )
            except ValueError:
                continue  # a relative link climbing out of the bundle

        if peer in seen or peer == concept_id:
            continue
        peer_file = (bundle / f"{peer}.md").resolve()
        if not peer_file.is_relative_to(bundle) or not peer_file.is_file():
            continue

        peer_doc = parse(peer_file.read_text(encoding="utf-8", errors="replace"))
        if peer_doc is None:
            continue
        seen.add(peer)
        out.append({
            "concept_id": peer,
            "title": str(peer_doc.frontmatter.get("title") or peer),
            "concept_type": str(peer_doc.frontmatter.get("type") or ""),
        })

    return out


def load_concept(cfg: Config, concept_id: str) -> dict | None:
    """One concept's detail, read from the bundle.

    Only for following a `related` link to a concept outside the current
    results. Search results already carry their own detail, because `Hit`
    brings the frontmatter and the source path with it.

    The id is a path fragment from an HTTP client, so it is treated as
    hostile: the resolved file must sit inside bundle/, which kills `../`
    before it reaches the filesystem.
    """
    from .okf import parse

    if not concept_id:
        return None

    # The filesystem gets to reject a path before any of our guards run. A
    # 400-character id raises OSError 63 (File name too long) and a percent
    # encoded null byte raises ValueError, both from resolve()/is_file(), and
    # both are reachable over HTTP as /api/concept?id=... An unhandled raise
    # here propagates out of the handler thread and drops the connection
    # instead of answering. An id the filesystem will not accept is a miss.
    try:
        bundle = cfg.bundle_dir.resolve()
        target = (bundle / f"{concept_id.strip('/')}.md").resolve()
        if not target.is_relative_to(bundle) or not target.is_file():
            return None
        raw = target.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None

    doc = parse(raw)
    if doc is None:
        return None

    fm = doc.frontmatter
    path = resolve_source_path(cfg, concept_id) or ""
    meta = file_meta(cfg, [path]).get(path, {})
    return {
        "related": _related(bundle, concept_id, doc.body),
        "concept_id": concept_id,
        "title": str(fm.get("title") or ""),
        "concept_type": str(fm.get("type") or ""),
        "sensitivity": str(fm.get("sensitivity") or ""),
        "description": str(fm.get("description") or ""),
        "tags": fm.get("tags") or [],
        "entities": [
            str(e.get("name", "")) if isinstance(e, dict) else str(e)
            for e in (fm.get("entities") or [])
        ],
        "file_path": path,
        "folder": meta.get("folder", ""),
        "mtime": meta.get("mtime"),
    }


def launch(path: str) -> None:
    """Hand a file to the OS opener.

    List form, so nothing goes through a shell. `path` never comes from a
    request body: it is resolved from the manifest by resolve_source_path.
    """
    subprocess.run(["open", path], check=False)


ICONS = {
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
    "/icon-256.png": ("icon-256.png", "image/png"),
}

STATIC_DIR = Path(__file__).parent / "static"
STATIC = STATIC_DIR / "index.html"


def render_page(cfg: Config) -> bytes:
    """The page, with the document types injected.

    The type dropdown comes from the closed set in config.toml. Injecting it
    here rather than adding another endpoint keeps the first paint to one
    request.
    """
    html = STATIC.read_text(encoding="utf-8")
    types = json.dumps(sorted(cfg.enrich.types))
    return html.replace("__TYPES__", types).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    cfg: Config = None  # set per-server by make_server

    def log_message(self, fmt, *args):  # noqa: A003
        """Silence per-request logging. `kb web` prints what matters."""

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: dict) -> None:
        self._send(status, json.dumps(obj).encode("utf-8"), "application/json")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, render_page(self.cfg), "text/html; charset=utf-8")
            return
        # Served from the package rather than linked from a CDN, so the page
        # still renders with the machine offline. Two fixed names, no path
        # from the request ever reaches the filesystem.
        if parsed.path in ICONS:
            name, ctype = ICONS[parsed.path]
            blob = (STATIC_DIR / name).read_bytes()
            self._send(200, blob, ctype)
            return
        if parsed.path == "/api/search":
            self._json(200, self._search(parse_qs(parsed.query)))
            return
        if parsed.path == "/api/concept":
            wanted = (parse_qs(parsed.query).get("id") or [""])[0]
            doc = load_concept(self.cfg, wanted)
            if doc is None:
                self._json(404, {"error": f"no concept {wanted}"})
                return
            self._json(200, doc)
            return
        self._json(404, {"error": f"no route {parsed.path}"})

    def _search(self, params: dict) -> dict:
        """Run the hybrid pipeline and shape it for the page.

        An empty query is an empty result, never an error: the page issues one
        on first paint before anything has been typed.
        """
        from .retrieve import search

        query = (params.get("q") or [""])[0].strip()
        if not query:
            return build_payload(self.cfg, [], {}, degraded="")

        hits, timings = search(
            self.cfg,
            query,
            concept_type_filter=(params.get("type") or [None])[0] or None,
            sensitivity_filter=(params.get("sensitivity") or [None])[0] or None,
            on_embed_error="degrade",
        )
        return build_payload(
            self.cfg, hits, timings, degraded=timings.get("degraded", "")
        )

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/open":
            self._json(404, {"error": f"no route {parsed.path}"})
            return

        # A non-numeric Content-Length used to raise here and drop the
        # connection instead of answering. Loopback and single-user, so low
        # severity, but a crashed handler thread is not a reply.
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._json(400, {"error": "Content-Length is not a number"})
            return

        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "body is not JSON"})
            return
        if not isinstance(body, dict):
            self._json(400, {"error": "body is not an object"})
            return

        # Only `id` is read. Any path in the body is ignored on purpose: this
        # server will not open a file of the caller's choosing.
        path = resolve_source_path(self.cfg, str(body.get("id") or ""))
        if not path:
            self._json(400, {"error": "unknown concept, nothing opened"})
            return
        if not Path(path).exists():
            self._json(400, {"error": f"source file is gone: {path}"})
            return

        launch(path)
        self._json(200, {"ok": True, "opened": path})


class _Server(HTTPServer):
    """Single-threaded on purpose, and the purpose is a crash.

    ThreadingHTTPServer creates and destroys a thread per request. Arrow's
    mimalloc allocator initialises a thread-local heap on first use in each new
    thread, and under that churn it faults:

        EXC_BAD_ACCESS SIGSEGV at 0x18, thread #42
        libarrow  mi_thread_init
        libarrow  arrow::MimallocAllocator::Allocate

    LanceDB and the graph both allocate through Arrow, so every search was
    exposed. Observed live: the process died mid-session and four consecutive
    requests failed with connection errors while launchd restarted it.

    Serialising requests removes the thread churn entirely. The cost is that
    two requests cannot overlap, which for one person on one machine is not a
    cost: the page issues a search, then a concept fetch after you click, never
    both at once.
    """

    daemon_threads = False
    allow_reuse_address = True


def make_server(cfg: Config, port: int = 8765) -> HTTPServer:
    """Bind to loopback only. This server is for one person on one machine."""
    handler = type("BoundHandler", (Handler,), {"cfg": cfg})
    return _Server(("127.0.0.1", port), handler)
