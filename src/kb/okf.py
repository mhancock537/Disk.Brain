"""OKF v0.2 primitives: frontmatter, concept files, index.md, log.md, validator.

Spec: github.com/GoogleCloudPlatform/knowledge-catalog `okf/SPEC.md`, pinned at
commit 3fcbb9f828c2f23d109c855ee403c3a4c81f3a96 (identical at origin/main
930b65fc3f5619d5d0591f88c72ebae8b848d60d, verified 2026-08-07).

Section numbers in comments refer to that document.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .config import get_logger

log = get_logger("okf")

OKF_VERSION = "0.2"
# Files at bundle root that are reports, not concepts, and carry no
# frontmatter. `entity-review.md` is written by `kb graph` (Phase 8C); without
# it here the 02:00 drain fails validation on a file the build just wrote.
RESERVED = {"index.md", "log.md", "entity-review.md"}
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)
LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
LOG_DATE_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})\s*$")


def utc_now_iso() -> str:
    """ISO 8601 with a Z suffix, the form used throughout the spec's examples."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(text: str, max_len: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(s) > max_len:
        s = s[:max_len].rsplit("-", 1)[0] or s[:max_len]
    return s or "untitled"


# --- frontmatter -------------------------------------------------------------


@dataclass
class Document:
    frontmatter: dict[str, Any]
    body: str

    @property
    def type(self) -> str | None:
        t = self.frontmatter.get("type")
        return t if isinstance(t, str) and t.strip() else None


def parse(text: str) -> Document | None:
    """Split a markdown file into frontmatter and body. None if unparseable."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    if fm is None:
        fm = {}
    if not isinstance(fm, dict):
        return None
    return Document(frontmatter=fm, body=m.group(2))


class _Dumper(yaml.SafeDumper):
    """Block style, no aliases, and quoted strings stay readable."""


def _str_presenter(dumper: yaml.Dumper, data: str):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _str_presenter)
_Dumper.ignore_aliases = lambda *_: True  # type: ignore[assignment]


def render(doc: Document) -> str:
    fm = yaml.dump(
        doc.frontmatter,
        Dumper=_Dumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=100,
    ).rstrip("\n")
    body = doc.body.strip("\n")
    return f"---\n{fm}\n---\n\n{body}\n"


def write_atomic(path: Path, text: str) -> None:
    """Write through a temp file and rename.

    A crash mid-write must leave the previous file intact, never a truncated
    one, because enrichment resume trusts the database rather than the disk.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --- concept ids -------------------------------------------------------------


def concept_id_for(path: Path, bundle_dir: Path) -> str:
    """Spec §2: the concept ID is the bundle path with `.md` removed."""
    return path.relative_to(bundle_dir).with_suffix("").as_posix()


def link_target(concept_id: str) -> str:
    """Spec §6.1 recommends the bundle-relative form, which begins with `/`."""
    return f"/{concept_id}.md"


def allocate_concept_id(
    concept_type: str, title: str, source_hash: str, taken: set[str]
) -> str:
    """`<type-slug>/<title-slug>`, disambiguated deterministically on collision.

    Collisions are common: a corpus holds many README.md and CLAUDE.md. The
    suffix comes from the source hash rather than a counter, so the same
    document lands on the same ID on every run.
    """
    directory = slugify(concept_type)
    base = slugify(title)
    if base in RESERVED or f"{base}.md" in RESERVED:
        base = f"{base}-concept"  # §3.1: index/log are reserved filenames

    candidate = f"{directory}/{base}"
    if candidate not in taken:
        return candidate
    for width in (6, 10, 16):
        candidate = f"{directory}/{base}-{source_hash[:width]}"
        if candidate not in taken:
            return candidate
    return f"{directory}/{base}-{source_hash}"


# --- index.md and log.md -----------------------------------------------------


@dataclass
class IndexEntry:
    title: str
    href: str
    description: str = ""


def render_index(
    sections: dict[str, list[IndexEntry]], okf_version: str | None = None
) -> str:
    """Spec §8. Index files carry no frontmatter, except a bundle-root
    `index.md`, which is the one place `okf_version` may appear."""
    out: list[str] = []
    if okf_version:
        out.append(f'---\nokf_version: "{okf_version}"\n---\n')
    for heading, entries in sections.items():
        out.append(f"# {heading}\n")
        for e in entries:
            desc = f" - {e.description}" if e.description else ""
            out.append(f"* [{e.title}]({e.href}){desc}")
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def prepend_log_entry(path: Path, day: date, lines: list[str], heading: str) -> None:
    """Spec §9: date-grouped, newest first, so a new run is prepended.

    An existing heading for the same day gains the new lines at its top.
    """
    stamp = day.isoformat()
    block = "\n".join(lines)
    if not path.exists():
        write_atomic(path, f"# {heading}\n\n## {stamp}\n{block}\n")
        return

    text = path.read_text(encoding="utf-8")
    marker = f"\n## {stamp}\n"
    if marker in text:
        head, rest = text.split(marker, 1)
        write_atomic(path, f"{head}{marker}{block}\n{rest.lstrip(chr(10))}")
        return

    first = text.find("\n## ")
    if first == -1:
        write_atomic(path, f"{text.rstrip()}\n\n## {stamp}\n{block}\n")
    else:
        write_atomic(path, f"{text[:first]}\n\n## {stamp}\n{block}\n{text[first + 1:]}")


# --- validator ---------------------------------------------------------------


@dataclass
class Finding:
    level: str  # "error" | "warning"
    path: str
    message: str


@dataclass
class ValidationReport:
    findings: list[Finding] = field(default_factory=list)
    concepts: int = 0
    indexes: int = 0
    logs: int = 0
    links: int = 0
    broken_links: int = 0

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, level: str, path: Path | str, message: str) -> None:
        self.findings.append(Finding(level, str(path), message))


# Recommended but not required by §4.1. Their absence is a warning at most.
RECOMMENDED_KEYS = ("title", "description")


def validate_bundle(bundle_dir: Path) -> ValidationReport:
    """Check conformance with §11.

    Errors are the three numbered conformance conditions. Everything the spec
    tells consumers they MUST NOT reject over (broken links, missing index
    files, missing optional keys) is a warning, never an error.
    """
    rep = ValidationReport()
    if not bundle_dir.is_dir():
        rep.add("error", bundle_dir, "bundle directory does not exist")
        return rep

    md_files = sorted(bundle_dir.rglob("*.md"))
    concept_ids: set[str] = set()

    for path in md_files:
        rel = path.relative_to(bundle_dir).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        name = path.name

        if name == "index.md":
            rep.indexes += 1
            _validate_index(path, rel, text, bundle_dir, rep)
            continue
        if name == "log.md":
            rep.logs += 1
            _validate_log(rel, text, rep)
            continue
        # Reports the build writes into the bundle, not concepts. They carry no
        # frontmatter by design, so §11.1 does not apply to them.
        if rel in RESERVED:
            continue

        # §11.1 and §11.2: every non-reserved .md needs parseable frontmatter
        # carrying a non-empty type.
        doc = parse(text)
        if doc is None:
            rep.add("error", rel, "no parseable YAML frontmatter block")
            continue
        if not doc.type:
            rep.add("error", rel, "frontmatter has no non-empty `type` field")
            continue

        rep.concepts += 1
        concept_ids.add(concept_id_for(path, bundle_dir))
        for key in RECOMMENDED_KEYS:
            if not doc.frontmatter.get(key):
                rep.add("warning", rel, f"missing recommended key `{key}`")

    _check_links(bundle_dir, md_files, concept_ids, rep)

    if not (bundle_dir / "index.md").exists():
        rep.add("warning", "index.md", "bundle root has no index.md (§8)")
    return rep


def _validate_index(
    path: Path, rel: str, text: str, bundle_dir: Path, rep: ValidationReport
) -> None:
    """§8: no frontmatter, except a root index.md that may carry okf_version."""
    is_root = path.parent == bundle_dir
    doc = parse(text)
    if doc is None:
        return
    if not is_root:
        rep.add("error", rel, "index.md must not contain frontmatter (§8)")
        return
    extra = set(doc.frontmatter) - {"okf_version"}
    if extra:
        rep.add(
            "error",
            rel,
            f"root index.md frontmatter may only hold okf_version, found {sorted(extra)}",
        )


def _validate_log(rel: str, text: str, rep: ValidationReport) -> None:
    """§9: date headings must be `## YYYY-MM-DD`, newest first."""
    dates: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            m = LOG_DATE_RE.match(line)
            if not m:
                rep.add("error", rel, f"log heading is not `## YYYY-MM-DD`: {line!r}")
            else:
                dates.append(m.group(1))
    if dates != sorted(dates, reverse=True):
        rep.add("warning", rel, "log date headings are not newest first (§9)")


def _check_links(
    bundle_dir: Path, md_files: list[Path], concept_ids: set[str], rep: ValidationReport
) -> None:
    """Broken links are warnings: §6.1 says consumers MUST tolerate them."""
    for path in md_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for _label, target in LINK_RE.findall(text):
            if target.startswith(("http://", "https://", "mailto:", "file://", "#")):
                continue
            rep.links += 1
            clean = target.split("#", 1)[0].split("?", 1)[0]
            if not clean:
                continue
            if clean.startswith("/"):
                resolved = bundle_dir / clean.lstrip("/")
            else:
                resolved = (path.parent / clean).resolve()
            if not resolved.exists():
                rep.broken_links += 1
                rep.add(
                    "warning",
                    path.relative_to(bundle_dir).as_posix(),
                    f"broken link: {target}",
                )
