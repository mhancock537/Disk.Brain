"""Property graph: node and edge extraction, bulk load, Cypher."""

from __future__ import annotations

import sqlite3

import pytest

from kb.graph import (
    EXAMPLE_QUERIES,
    build_graph,
    collect,
    duplicate_candidates,
    entity_key,
    query,
    write_entity_review,
)

CONCEPT = """\
---
type: {type}
title: {title}
description: A description of {title}.
resource: file:///Users/example/Documents/{slug}.md
tags: {tags}
status: stable
entities: {entities}
sensitivity: work
source_hash: {h}
ingest_run: run1
generated:
  by: okf-kb/test-model
  at: '{at}'
---

# Summary

Text about {title}.

# Related

{links}
"""


def write_concept(bundle, path, *, type_, title, tags, entities, h, at, links=""):
    p = bundle / f"{path}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        CONCEPT.format(
            type=type_, title=title, slug=path.replace("/", "-"),
            tags=str(tags), entities=str(entities), h=h, at=at, links=links,
        )
    )
    return p


@pytest.fixture
def graph_bundle(cfg):
    b = cfg.bundle_dir
    b.mkdir(parents=True, exist_ok=True)
    write_concept(
        b, "runbook/restart", type_="Runbook", title="Restart", tags=["ops", "widget"],
        entities=[{"name": "Acme Corp", "kind": "organization"},
                  {"name": "Bob Smith", "kind": "person"}],
        h="aa11", at="2026-08-01T00:00:00Z",
        links="* [Rotate](/runbook/rotate.md)\n* [Gone](/runbook/missing.md)",
    )
    write_concept(
        b, "runbook/rotate", type_="Runbook", title="Rotate", tags=["ops"],
        entities=[{"name": "ACME CORP", "kind": "organization"}],
        h="bb22", at="2026-08-02T00:00:00Z",
    )
    write_concept(
        b, "report/market", type_="Report", title="Market", tags=["research"],
        entities=[{"name": "Bob Smith", "kind": "person"},
                  {"name": "Acme Corp", "kind": "project"}],
        h="cc33", at="2026-07-15T00:00:00Z",
    )
    (b / "index.md").write_text('---\nokf_version: "0.2"\n---\n\n# All\n')
    (b / "log.md").write_text("# Log\n\n## 2026-08-07\n* **Creation**: made.\n")
    (b / "runbook" / "index.md").write_text("# Runbook\n\n* [R](restart.md)\n")

    # Manifest rows so File nodes and DERIVED_FROM edges have something to bind to.
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.manifest_path)
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, hash TEXT, "
        "ext TEXT, size INTEGER, mtime REAL);"
    )
    conn.executemany(
        "INSERT INTO files (path, hash, ext, size, mtime) VALUES (?, ?, ?, ?, ?)",
        [("/a/restart.md", "aa11", ".md", 100, 1.0),
         ("/a/rotate.md", "bb22", ".md", 200, 2.0),
         ("/a/market.pdf", "cc33", ".pdf", 300, 3.0)],
    )
    conn.commit()
    conn.close()
    return b


# --- entity keys -------------------------------------------------------------


def test_entity_key_is_casefolded_and_kind_scoped():
    assert entity_key("Acme Corp", "organization") == entity_key("ACME CORP", "organization")
    assert entity_key("Acme  Corp", "organization") == entity_key("Acme Corp", "organization")
    assert entity_key("Acme", "organization") != entity_key("Acme", "project")


def test_distinct_surface_forms_stay_distinct():
    """Phase 8C requires separate nodes per surface form, never a merge."""
    assert entity_key("Ada", "person") != entity_key("Ada Lovelace", "person")


# --- collection --------------------------------------------------------------


def test_collect_builds_every_node_type(cfg, graph_bundle):
    d = collect(cfg)
    assert len(d.concepts) == 3
    assert len(d.files) == 3
    assert {t["name"] for t in d.tags} == {"ops", "widget", "research"}
    keys = {e["key"] for e in d.entities}
    assert entity_key("Acme Corp", "organization") in keys
    assert entity_key("Acme Corp", "project") in keys  # same name, other kind


def test_collect_counts_entity_occurrences(cfg, graph_bundle):
    d = collect(cfg)
    by_key = {e["key"]: e for e in d.entities}
    assert by_key[entity_key("Acme Corp", "organization")]["occurrence_count"] == 2
    assert by_key[entity_key("Bob Smith", "person")]["occurrence_count"] == 2


def test_collect_takes_the_earliest_first_seen(cfg, graph_bundle):
    d = collect(cfg)
    bob = next(e for e in d.entities if e["key"] == entity_key("Bob Smith", "person"))
    assert bob["first_seen"] == "2026-07-15T00:00:00Z"


def test_collect_keeps_the_display_form_of_the_first_entity_seen(cfg, graph_bundle):
    d = collect(cfg)
    acme = next(
        e for e in d.entities if e["key"] == entity_key("Acme Corp", "organization")
    )
    assert acme["name"] in ("Acme Corp", "ACME CORP")


# --- Phase 8C: entity properties, duplicate report, aliases ------------------


def test_entity_carries_its_surface_form(cfg, graph_bundle):
    """8C names the display field `surface_form`. `name` stays as an alias."""
    d = collect(cfg)
    acme = next(
        e for e in d.entities if e["key"] == entity_key("Acme Corp", "organization")
    )
    assert acme["surface_form"] == acme["name"]
    assert acme["surface_form"] in ("Acme Corp", "ACME CORP")


def test_entity_records_the_sources_it_came_from(cfg, graph_bundle):
    """8C requires a `sources` property. Everything is `local` until 8A lands."""
    d = collect(cfg)
    bob = next(e for e in d.entities if e["key"] == entity_key("Bob Smith", "person"))
    assert bob["sources"] == "local"


def _ents(*pairs):
    """Minimal entity rows: (surface_form, kind, occurrence_count)."""
    return [
        {"key": entity_key(n, k), "name": n, "surface_form": n, "kind": k,
         "sources": "local", "first_seen": "", "occurrence_count": c}
        for n, k, c in pairs
    ]


def test_duplicate_candidates_flags_a_name_contained_in_another():
    """ROADMAP's own example: `Dana Reyes` and `Dana` are two nodes, one person."""
    cands = duplicate_candidates(
        _ents(("Dana Reyes", "person", 9), ("Dana", "person", 3)), []
    )
    assert len(cands) == 1
    assert {cands[0]["a"], cands[0]["b"]} == {"Dana Reyes", "Dana"}
    assert "contained" in cands[0]["reason"]


def test_duplicate_candidates_matches_an_email_local_part_to_a_person():
    cands = duplicate_candidates(
        _ents(("Dana Reyes", "person", 9), ("dreyes@example.com", "person", 2)), []
    )
    assert len(cands) == 1
    assert "email" in cands[0]["reason"]


def test_duplicate_candidates_survive_a_single_token_name():
    """A one-word name leaves no other tokens to build an initial from."""
    cands = duplicate_candidates(
        _ents(("Dana", "person", 4), ("dreyes@example.com", "person", 2)), []
    )
    assert isinstance(cands, list)


def test_duplicate_candidates_never_pairs_across_kinds():
    """`Acme` the organization and `Acme` the project are deliberately separate."""
    assert duplicate_candidates(
        _ents(("Acme Corp", "organization", 5), ("Acme Corp", "project", 2)), []
    ) == []


def test_duplicate_candidates_are_sorted_by_occurrence_count():
    cands = duplicate_candidates(
        _ents(("Bob Smith", "person", 2), ("Bob", "person", 1),
              ("Jane Doe", "person", 40), ("Jane", "person", 6)),
        [],
    )
    assert [c["a"] for c in cands] == ["Jane Doe", "Bob Smith"]


ALIASES = """\
[[alias]]
kind = "person"
canonical = "Bob Smith"
forms = ["Bob", "bsmith@example.com"]
"""


def test_without_an_alias_file_surface_forms_stay_separate(cfg, graph_bundle):
    write_concept(
        graph_bundle, "runbook/extra", type_="Runbook", title="Extra", tags=["ops"],
        entities=[{"name": "Bob", "kind": "person"}],
        h="dd44", at="2026-08-03T00:00:00Z",
    )
    keys = {e["key"] for e in collect(cfg).entities}
    assert entity_key("Bob", "person") in keys
    assert entity_key("Bob Smith", "person") in keys


def test_aliases_merge_surface_forms_at_graph_build(cfg, graph_bundle):
    write_concept(
        graph_bundle, "runbook/extra", type_="Runbook", title="Extra", tags=["ops"],
        entities=[{"name": "Bob", "kind": "person"}],
        h="dd44", at="2026-08-03T00:00:00Z",
    )
    (cfg.aliases_path).parent.mkdir(parents=True, exist_ok=True)
    cfg.aliases_path.write_text(ALIASES)

    d = collect(cfg)
    keys = {e["key"] for e in d.entities}
    assert entity_key("Bob", "person") not in keys, "the alias should have folded in"
    bob = next(e for e in d.entities if e["key"] == entity_key("Bob Smith", "person"))
    assert bob["occurrence_count"] == 3
    assert bob["surface_form"] == "Bob Smith"


def test_aliases_are_reversible_by_removing_the_file(cfg, graph_bundle):
    """The merge lives in the alias file, never in the bundle. Delete, rebuild."""
    write_concept(
        graph_bundle, "runbook/extra", type_="Runbook", title="Extra", tags=["ops"],
        entities=[{"name": "Bob", "kind": "person"}],
        h="dd44", at="2026-08-03T00:00:00Z",
    )
    cfg.aliases_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.aliases_path.write_text(ALIASES)
    assert entity_key("Bob", "person") not in {e["key"] for e in collect(cfg).entities}
    cfg.aliases_path.unlink()
    assert entity_key("Bob", "person") in {e["key"] for e in collect(cfg).entities}


def test_the_canonical_spelling_comes_from_the_alias_file(cfg, graph_bundle):
    """Not from whichever concept happened to be read first."""
    write_concept(
        graph_bundle, "runbook/extra", type_="Runbook", title="Extra", tags=["ops"],
        entities=[{"name": "Bob", "kind": "person"}],
        h="dd44", at="2026-08-03T00:00:00Z",
    )
    cfg.aliases_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.aliases_path.write_text(
        '[[alias]]\nkind = "person"\ncanonical = "Robert Smith"\n'
        'forms = ["Bob Smith", "Bob"]\n'
    )
    d = collect(cfg)
    bob = next(
        e for e in d.entities if e["key"] == entity_key("Robert Smith", "person")
    )
    assert bob["surface_form"] == "Robert Smith"
    assert bob["occurrence_count"] == 3


def test_an_alias_never_crosses_kinds(cfg, graph_bundle):
    """`Acme Corp` the project must not fold into `Acme Corp` the organization."""
    cfg.aliases_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.aliases_path.write_text(
        '[[alias]]\nkind = "organization"\ncanonical = "Acme Corp"\n'
        'forms = ["Acme Corp"]\n'
    )
    keys = {e["key"] for e in collect(cfg).entities}
    assert entity_key("Acme Corp", "project") in keys


def test_entity_review_is_written_to_the_bundle(cfg, graph_bundle):
    d = collect(cfg)
    path = write_entity_review(cfg, d)
    assert path == cfg.bundle_dir / "entity-review.md"
    text = path.read_text()
    assert "never merged automatically" in text
    assert "entity-aliases.toml" in text


def test_collect_links_only_to_existing_concepts(cfg, graph_bundle):
    d = collect(cfg)
    assert {(l["from"], l["to"]) for l in d.links_to} == {
        ("runbook/restart", "runbook/rotate")
    }


def test_broken_links_are_recorded_not_fatal(cfg, graph_bundle):
    d = collect(cfg)
    assert len(d.broken_links) == 1
    assert d.broken_links[0][1].endswith("missing.md")


def test_derived_from_binds_concepts_to_files(cfg, graph_bundle):
    d = collect(cfg)
    assert {(e["from"], e["to"]) for e in d.derived_from} == {
        ("runbook/restart", "aa11"),
        ("runbook/rotate", "bb22"),
        ("report/market", "cc33"),
    }


def test_collect_ignores_reserved_files(cfg, graph_bundle):
    assert all("index" not in c["concept_id"] for c in collect(cfg).concepts)


def test_collect_refuses_a_missing_bundle(cfg):
    with pytest.raises(RuntimeError, match="no bundle"):
        collect(cfg)


def test_child_of_follows_bundle_nesting(cfg, graph_bundle):
    """CHILD_OF is empty on a flat tree and populated on a nested one."""
    assert collect(cfg).child_of == []

    write_concept(
        cfg.bundle_dir, "runbook/restart/step-one", type_="Runbook",
        title="Step One", tags=[], entities=[], h="dd44", at="2026-08-03T00:00:00Z",
    )
    edges = {(e["from"], e["to"]) for e in collect(cfg).child_of}
    assert ("runbook/restart/step-one", "runbook/restart") in edges


# --- build and query ---------------------------------------------------------


def test_build_graph_counts(cfg, graph_bundle):
    counts = build_graph(cfg)
    # Entities: Acme Corp/org (twice, one node), Bob Smith/person,
    # Acme Corp/project. Same name, different kind, so three nodes.
    assert counts["nodes"] == {"Concept": 3, "File": 3, "Entity": 3, "Tag": 3}
    assert counts["edges"]["LINKS_TO"] == 1
    assert counts["edges"]["DERIVED_FROM"] == 3
    assert counts["edges"]["MENTIONS"] == 5
    assert counts["edges"]["TAGGED_AS"] == 4
    assert counts["broken_links"] == 1


def test_build_graph_is_repeatable(cfg, graph_bundle):
    first = build_graph(cfg)
    second = build_graph(cfg)
    assert first["nodes"] == second["nodes"]
    assert first["edges"] == second["edges"]


def test_build_graph_removes_its_staging_directory(cfg, graph_bundle):
    build_graph(cfg)
    assert not (cfg.data_dir / "graph_staging").exists()


def test_rebuild_does_not_accumulate(cfg, graph_bundle):
    """A second build must replace the store, not append to it."""
    build_graph(cfg)
    (cfg.bundle_dir / "runbook" / "rotate.md").unlink()
    counts = build_graph(cfg)
    assert counts["nodes"]["Concept"] == 2


def test_duplicate_edges_are_collapsed(cfg, graph_bundle):
    """The same tag twice on one concept must not double the edge count."""
    p = cfg.bundle_dir / "runbook" / "rotate.md"
    p.write_text(p.read_text().replace("tags: ['ops']", "tags: ['ops', 'ops']"))
    assert build_graph(cfg)["edges"]["TAGGED_AS"] == 4


@pytest.mark.parametrize("name", list(EXAMPLE_QUERIES))
def test_example_queries_run(cfg, graph_bundle, name):
    build_graph(cfg)
    df = query(cfg, EXAMPLE_QUERIES[name])
    assert df is not None  # a zero-row result is still a valid answer


def test_query_returns_expected_rows(cfg, graph_bundle):
    build_graph(cfg)
    df = query(
        cfg,
        "MATCH (c:Concept)-[:MENTIONS]->(e:Entity) WHERE e.kind = 'person' "
        "RETURN e.name AS person, count(c) AS documents ORDER BY documents DESC",
    )
    assert df.iloc[0]["person"] == "Bob Smith"
    assert int(df.iloc[0]["documents"]) == 2


def test_concept_properties_survive_the_round_trip(cfg, graph_bundle):
    build_graph(cfg)
    df = query(
        cfg,
        "MATCH (c:Concept {concept_id: 'runbook/restart'}) "
        "RETURN c.title AS title, c.sensitivity AS s, c.source_hash AS h, "
        "c.generated_by AS by, c.directory AS d",
    )
    row = df.iloc[0]
    assert row["title"] == "Restart"
    assert row["s"] == "work"
    assert row["h"] == "aa11"
    assert row["by"] == "okf-kb/test-model"
    assert row["d"] == "runbook"


def test_query_without_a_graph_is_a_clear_error(cfg):
    with pytest.raises(FileNotFoundError, match="kb graph"):
        query(cfg, "MATCH (n) RETURN n")


def test_filename_entities_are_kept_out_of_the_graph(cfg, graph_bundle):
    """The enricher is fixed, but 2,430 concepts already have filenames baked
    into their frontmatter and re-enriching is a twelve-hour GPU run.

    The graph rebuilds in seconds, so filtering here cleans it now. Same
    predicate as the enricher, so the two cannot disagree about what an entity
    is.
    """
    write_concept(
        graph_bundle, "runbook/filey", type_="Runbook", title="Filey", tags=["ops"],
        entities=[{"name": "README.md", "kind": "system"},
                  {"name": "src/app.py", "kind": "system"},
                  {"name": "https://example.com", "kind": "organization"},
                  {"name": "Next.js", "kind": "system"},
                  {"name": "Redwood", "kind": "system"}],
        h="ee55", at="2026-08-10T00:00:00Z",
    )
    forms = {e["surface_form"] for e in collect(cfg).entities}

    assert "README.md" not in forms
    assert "src/app.py" not in forms
    assert "https://example.com" not in forms
    assert "Next.js" in forms, "a real technology must survive"
    assert "Redwood" in forms
