"""Bundle writing: cross-link scoring, concept rendering, index and log files."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from kb.bundle import (
    Concept,
    build_document,
    find_orphans,
    load_concepts,
    related_map,
    write_bundle,
)
from kb.enrich import Record, run_enrich
from kb.extract.runner import run_extract
from kb.manifest import Manifest, scan
from kb.okf import parse, validate_bundle


def make_concept(cid: str, tags: list[str], entities: list[tuple[str, str]], **kw) -> Concept:
    base = dict(
        source_hash=cid.replace("/", "") + "0" * 8,
        concept_id=cid,
        concept_type=cid.split("/")[0].replace("-", " ").title(),
        title=cid.split("/")[-1].replace("-", " ").title(),
        description="A description.",
        tags=tags,
        entities=[{"name": n, "kind": k} for n, k in entities],
        sensitivity="work",
        status="stable",
        model="test-model",
        generated_at="2026-08-07T00:00:00Z",
        ingest_run="run1",
        source_path="/Users/example/Documents/a file.md",
        ext=".md",
        size=100,
        mtime=1_750_000_000.0,
        word_count=42,
    )
    base.update(kw)
    return Concept(**base)


# --- cross-link scoring ------------------------------------------------------


def test_rare_shared_entity_creates_a_link(cfg):
    a = make_concept("note/a", [], [("Zeta Corp", "organization")])
    b = make_concept("note/b", [], [("Zeta Corp", "organization")])
    links = related_map(cfg, [a, b])
    assert links["note/a"][0][0] == "note/b"


def test_a_term_on_everything_creates_no_link(cfg):
    """A tag shared by more concepts than the ceiling is background noise."""
    tiny = replace(cfg, bundle=replace(cfg.bundle, link_rarity_ceiling=3))
    concepts = [make_concept(f"note/n{i}", ["common"], []) for i in range(6)]
    assert related_map(tiny, concepts) == {}


def test_entities_outrank_tags(cfg):
    target = make_concept("note/target", ["shared"], [("Rare Co", "organization")])
    by_entity = make_concept("note/by-entity", [], [("Rare Co", "organization")])
    by_tag = make_concept("note/by-tag", ["shared"], [])
    ranked = related_map(cfg, [target, by_entity, by_tag])["note/target"]
    assert ranked[0][0] == "note/by-entity"
    assert ranked[0][1] > ranked[1][1]


def test_unrelated_concepts_get_no_links(cfg):
    a = make_concept("note/a", ["alpha"], [("A Co", "organization")])
    b = make_concept("note/b", ["beta"], [("B Co", "organization")])
    assert related_map(cfg, [a, b]) == {}


def test_links_are_capped_at_max_related(cfg):
    capped = replace(cfg, bundle=replace(cfg.bundle, max_related=2))
    concepts = [make_concept(f"note/n{i}", [], [("Shared Co", "organization")]) for i in range(8)]
    links = related_map(capped, concepts)
    assert all(len(v) <= 2 for v in links.values())


def test_link_scoring_is_symmetric_and_self_free(cfg):
    concepts = [make_concept(f"note/n{i}", ["t"], [("E", "person")]) for i in range(3)]
    links = related_map(cfg, concepts)
    for cid, peers in links.items():
        assert cid not in dict(peers)
    assert dict(links["note/n0"])["note/n1"] == pytest.approx(
        dict(links["note/n1"])["note/n0"]
    )


def test_same_name_different_kind_does_not_match(cfg):
    a = make_concept("note/a", [], [("Falcon", "project")])
    b = make_concept("note/b", [], [("Falcon", "system")])
    assert related_map(cfg, [a, b]) == {}


def test_entity_matching_is_case_insensitive(cfg):
    a = make_concept("note/a", [], [("Acme Corp", "organization")])
    b = make_concept("note/b", [], [("ACME CORP", "organization")])
    assert "note/b" in dict(related_map(cfg, [a, b])["note/a"])


# --- concept rendering -------------------------------------------------------


def test_document_frontmatter_carries_every_required_key(cfg):
    c = make_concept("runbook/a", ["x"], [("Acme", "organization")])
    doc = build_document(cfg, c, [], {c.concept_id: c})
    fm = doc.frontmatter
    assert fm["type"] == c.concept_type
    assert fm["title"] and fm["description"]
    assert fm["sensitivity"] == "work"
    assert fm["source_hash"] == c.source_hash
    assert fm["ingest_run"] == "run1"
    assert fm["generated"]["by"] == "okf-kb/test-model"
    # v0.2 supersedes `timestamp` with `generated.at`; both are written.
    assert fm["timestamp"] == fm["generated"]["at"]


def test_resource_uri_percent_encodes_spaces(cfg):
    c = make_concept("runbook/a", [], [], source_path="/Users/example/My Projects/a b.md")
    fm = build_document(cfg, c, [], {c.concept_id: c}).frontmatter
    assert fm["resource"] == "file:///Users/example/My%20Projects/a%20b.md"
    assert fm["sources"][0]["resource"] == fm["resource"]


def test_related_section_uses_bundle_relative_links(cfg):
    a = make_concept("note/a", ["shared"], [])
    b = make_concept("note/b", ["shared"], [])
    doc = build_document(cfg, a, [("note/b", 0.9)], {"note/a": a, "note/b": b})
    assert "(/note/b.md)" in doc.body
    assert "# Related" in doc.body


def test_entities_section_groups_by_kind(cfg):
    c = make_concept("note/a", [], [("Bob", "person"), ("Acme", "organization")])
    body = build_document(cfg, c, [], {c.concept_id: c}).body
    assert "- **Person**: Bob" in body
    assert "- **Organization**: Acme" in body


def test_rendered_concept_parses_back(cfg):
    from kb.okf import render

    c = make_concept("note/a", ["x"], [("Acme", "organization")])
    doc = build_document(cfg, c, [], {c.concept_id: c})
    back = parse(render(doc))
    assert back is not None and back.type == c.concept_type


# --- the write pass ----------------------------------------------------------


@pytest.fixture
def enriched(cfg, tmp_path, monkeypatch):
    """A real scan and extract, with the LLM call stubbed out."""
    monkeypatch.setattr("kb.enrich.check_model", lambda c: (True, "test-model"))

    types = ["Runbook", "Reference", "Report"]
    state = {"i": 0}

    def fake(c, filename, path, text):
        i = state["i"]
        state["i"] += 1
        return Record(
            title=f"Concept {i}",
            description=f"Description number {i}.",
            concept_type=types[i % len(types)],
            tags=["alpha"] if i % 2 == 0 else ["beta"],
            entities=[{"name": "Acme Corp", "kind": "organization"}] if i % 3 == 0 else [],
            sensitivity="work",
        )

    monkeypatch.setattr("kb.enrich.enrich_one", fake)
    mf = Manifest(tmp_path / "m.db")
    scan(cfg, mf, do_hash=True)
    run_extract(cfg, mf, show_progress=False)
    run_enrich(cfg, mf, show_progress=False)
    yield mf
    mf.close()


def test_write_bundle_produces_a_conformant_bundle(cfg, enriched):
    stats, report = write_bundle(cfg, enriched, show_progress=False)
    assert stats["concepts"] > 0
    assert report.ok, [f.message for f in report.errors]
    assert report.concepts == stats["concepts"]


def test_bundle_tree_is_organised_by_type(cfg, enriched):
    write_bundle(cfg, enriched, show_progress=False)
    dirs = {p.name for p in cfg.bundle_dir.iterdir() if p.is_dir()}
    assert dirs <= {"runbook", "reference", "report"}
    assert dirs


def test_every_directory_gets_an_index_without_frontmatter(cfg, enriched):
    write_bundle(cfg, enriched, show_progress=False)
    for d in [p for p in cfg.bundle_dir.iterdir() if p.is_dir()]:
        idx = d / "index.md"
        assert idx.exists()
        assert not idx.read_text().startswith("---")


def test_root_index_declares_the_okf_version(cfg, enriched):
    write_bundle(cfg, enriched, show_progress=False)
    assert 'okf_version: "0.2"' in (cfg.bundle_dir / "index.md").read_text()


def test_log_is_written_with_an_iso_heading(cfg, enriched):
    write_bundle(cfg, enriched, show_progress=False)
    text = (cfg.bundle_dir / "log.md").read_text()
    assert "# Bundle Update Log" in text
    import re

    assert re.search(r"^## \d{4}-\d{2}-\d{2}$", text, re.M)


def test_writing_twice_is_idempotent(cfg, enriched):
    first, _ = write_bundle(cfg, enriched, show_progress=False)
    files_before = {p: p.read_text() for p in cfg.bundle_dir.rglob("*.md")}
    second, report = write_bundle(cfg, enriched, show_progress=False)
    assert second["concepts"] == first["concepts"]
    assert report.ok
    # Only log.md gains a line; concepts and indexes are byte-identical.
    for p, text in files_before.items():
        if p.name != "log.md":
            assert p.read_text() == text


def test_written_at_is_recorded(cfg, enriched):
    write_bundle(cfg, enriched, show_progress=False)
    rows = enriched.conn.execute(
        "SELECT written_at FROM concepts WHERE enrich_status='ok'"
    ).fetchall()
    assert all(r["written_at"] for r in rows)


def test_empty_bundle_validates_as_missing_not_crash(cfg, tmp_path):
    with Manifest(tmp_path / "m.db") as mf:
        stats, report = write_bundle(cfg, mf, show_progress=False)
        assert stats["concepts"] == 0
        assert not report.ok  # the directory does not exist yet


# --- orphan reconciliation ---------------------------------------------------


def test_orphans_are_reported_but_not_deleted_by_default(cfg, enriched):
    write_bundle(cfg, enriched, show_progress=False)
    stale = cfg.bundle_dir / "runbook" / "gone-forever.md"
    stale.write_text("---\ntype: Runbook\ntitle: Gone\ndescription: d\n---\n\n# B\n")

    stats, report = write_bundle(cfg, enriched, show_progress=False)
    assert stats["orphans_found"] == 1
    assert stats["orphans_pruned"] == 0
    assert stale.exists()  # model output is never deleted as a side effect
    assert report.ok


def test_prune_removes_orphans(cfg, enriched):
    write_bundle(cfg, enriched, show_progress=False)
    stale = cfg.bundle_dir / "runbook" / "gone-forever.md"
    stale.write_text("---\ntype: Runbook\ntitle: Gone\ndescription: d\n---\n\n# B\n")

    stats, report = write_bundle(cfg, enriched, show_progress=False, prune=True)
    assert stats["orphans_pruned"] == 1
    assert not stale.exists()
    assert report.ok


def test_prune_removes_a_directory_that_lost_every_concept(cfg, enriched):
    write_bundle(cfg, enriched, show_progress=False)
    dead = cfg.bundle_dir / "obsolete-type"
    dead.mkdir()
    (dead / "old.md").write_text("---\ntype: Obsolete\ntitle: O\ndescription: d\n---\n\n# B\n")
    (dead / "index.md").write_text("# Obsolete\n\n* [O](old.md)\n")

    write_bundle(cfg, enriched, show_progress=False, prune=True)
    assert not dead.exists()


def test_prune_keeps_reserved_files_and_live_concepts(cfg, enriched):
    write_bundle(cfg, enriched, show_progress=False)
    live = sorted(
        p for p in cfg.bundle_dir.rglob("*.md") if p.name not in {"index.md", "log.md"}
    )
    write_bundle(cfg, enriched, show_progress=False, prune=True)
    assert all(p.exists() for p in live)
    assert (cfg.bundle_dir / "log.md").exists()
    assert (cfg.bundle_dir / "index.md").exists()


def test_find_orphans_ignores_reserved_names(cfg, enriched):
    from kb.bundle import find_orphans

    write_bundle(cfg, enriched, show_progress=False)
    assert find_orphans(cfg.bundle_dir, set()) and all(
        p.name not in {"index.md", "log.md"}
        for p in find_orphans(cfg.bundle_dir, set())
    )


# --- find_orphans must honour its own contract -------------------------------


def test_the_entity_review_report_is_never_an_orphan(cfg):
    """It is a build artefact `kb graph` writes, not a concept.

    find_orphans hardcoded {index.md, log.md} instead of reading okf.RESERVED,
    so adding entity-review.md to RESERVED for the validator left this path
    behind. `kb bundle --prune` would have deleted the 8C duplicate report.
    """
    b = cfg.bundle_dir
    b.mkdir(parents=True, exist_ok=True)
    (b / "entity-review.md").write_text("# Entity review\n\nNo frontmatter.\n")
    (b / "index.md").write_text("# All\n")
    (b / "log.md").write_text("# Log\n")

    names = {p.name for p in find_orphans(b, set())}
    assert "entity-review.md" not in names
    assert "index.md" not in names
    assert "log.md" not in names


def test_a_deprecated_concept_is_never_an_orphan(cfg):
    """Phase 7's contract: a deleted source deprecates its concept and the
    concept file stays for links and history. The docstring said so; the code
    never checked, so prune would delete exactly the files the design protects.
    """
    b = cfg.bundle_dir / "runbook"
    b.mkdir(parents=True, exist_ok=True)
    (b / "gone.md").write_text(
        "---\ntype: Runbook\ntitle: Gone\ndescription: d\n"
        "status: deprecated\ndeprecated_at: '2026-08-07T00:00:00Z'\n---\n\n# Body\n"
    )
    (b / "live-but-unclaimed.md").write_text(
        "---\ntype: Runbook\ntitle: Live\ndescription: d\nstatus: stable\n---\n\n# Body\n"
    )

    orphans = {p.name for p in find_orphans(cfg.bundle_dir, set())}
    assert "gone.md" not in orphans, "deprecated concepts must survive prune"
    assert "live-but-unclaimed.md" in orphans, "a genuine orphan must still be found"


def test_an_unparseable_file_is_still_an_orphan(cfg):
    """No frontmatter means no deprecation claim, so it stays prunable."""
    b = cfg.bundle_dir / "runbook"
    b.mkdir(parents=True, exist_ok=True)
    (b / "junk.md").write_text("not a concept at all\n")
    assert "junk.md" in {p.name for p in find_orphans(cfg.bundle_dir, set())}
