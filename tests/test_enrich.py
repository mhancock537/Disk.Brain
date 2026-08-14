"""Enrichment: prompt shape, output hygiene, sensitivity rules, resume."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from kb.config import EnrichConfig
from kb.enrich import (
    ENTITY_KINDS,
    is_file_like,
    Record,
    _clean,
    build_prompt,
    response_schema,
    run_enrich,
    sensitivity_from_path,
)
from kb.manifest import Manifest, scan


@pytest.fixture
def ec() -> EnrichConfig:
    return EnrichConfig(
        model="test-model",
        prompt_words=50,
        temperature=0.2,
        num_ctx=4096,
        timeout_seconds=60,
        max_tags=3,
        max_entities=4,
        max_attempts=2,
        types={"Runbook": "a procedure", "Reference": "lookup", "Other": "none fit"},
        sensitivity_default="personal",
        work_globs=["*/Projects/*"],
        personal_globs=["*/Documents/Health/*"],
    )


# --- schema and prompt -------------------------------------------------------


def test_schema_locks_type_to_the_configured_set(ec):
    s = response_schema(ec)
    assert s["properties"]["concept_type"]["enum"] == ["Runbook", "Reference", "Other"]
    assert s["properties"]["tags"]["maxItems"] == 3
    assert s["properties"]["entities"]["maxItems"] == 4
    assert set(s["required"]) == {
        "title", "description", "concept_type", "tags", "entities", "sensitivity",
    }


def test_schema_entity_kinds_match_the_graph_vocabulary(ec):
    kinds = response_schema(ec)["properties"]["entities"]["items"]["properties"]["kind"]
    assert kinds["enum"] == list(ENTITY_KINDS)


def test_prompt_truncates_to_prompt_words(ec):
    text = " ".join(f"w{i}" for i in range(500))
    p = build_prompt(ec, "a.md", "/x/a.md", text)
    assert "w49" in p and "w50" not in p


def test_prompt_carries_the_type_menu_with_descriptions(ec):
    p = build_prompt(ec, "a.md", "/x/a.md", "hello")
    assert "- Runbook: a procedure" in p
    assert "- Reference: lookup" in p
    assert "a.md" in p and "/x/a.md" in p


# --- output hygiene ----------------------------------------------------------


def test_clean_normalises_tags(ec):
    r = _clean(
        {"title": "T", "description": "d", "concept_type": "Runbook",
         "tags": ["  Alpha ", "ALPHA", "beta", "gamma", "delta"],
         "entities": [], "sensitivity": "work"},
        ec, "fallback",
    )
    assert r.tags == ["alpha", "beta", "gamma"]  # deduped, lowercased, capped at 3


def test_clean_drops_bad_entities_and_dedupes(ec):
    r = _clean(
        {"title": "T", "description": "d", "concept_type": "Runbook",
         "entities": [
             {"name": "Acme", "kind": "organization"},
             {"name": "ACME", "kind": "organization"},   # same casefolded key
             {"name": "Acme", "kind": "wizard"},          # invalid kind
             {"name": "", "kind": "person"},              # empty name
             {"name": "Bob", "kind": "person"},
             "not-a-dict",
         ],
         "tags": [], "sensitivity": "work"},
        ec, "fallback",
    )
    assert r.entities == [
        {"name": "Acme", "kind": "organization"},
        {"name": "Bob", "kind": "person"},
    ]


def test_clean_falls_back_on_empty_title(ec):
    r = _clean({"title": "   ", "description": "d", "concept_type": "Runbook",
                "tags": [], "entities": [], "sensitivity": "work"}, ec, "my-file")
    assert r.title == "my-file"


def test_clean_forces_an_out_of_set_type_to_other(ec):
    r = _clean({"title": "T", "description": "d", "concept_type": "Invented",
                "tags": [], "entities": [], "sensitivity": "work"}, ec, "f")
    assert r.concept_type == "Other"


def test_clean_falls_back_on_bad_sensitivity(ec):
    r = _clean({"title": "T", "description": "d", "concept_type": "Runbook",
                "tags": [], "entities": [], "sensitivity": "classified"}, ec, "f")
    assert r.sensitivity == "personal"


def test_clean_collapses_whitespace_in_description(ec):
    r = _clean({"title": "T", "description": "a\n\n  b   c", "concept_type": "Runbook",
                "tags": [], "entities": [], "sensitivity": "work"}, ec, "f")
    assert r.description == "a b c"


# --- sensitivity rules -------------------------------------------------------


def test_sensitivity_path_rules(ec):
    assert sensitivity_from_path(ec, "/Users/m/Projects/x/a.md") == "work"
    assert sensitivity_from_path(ec, "/Users/m/Documents/Health/a.md") == "personal"
    assert sensitivity_from_path(ec, "/Users/m/Desktop/a.md") is None


def test_personal_globs_win_over_work_globs(ec):
    both = replace(ec, work_globs=["*/x/*"], personal_globs=["*/x/*"])
    assert sensitivity_from_path(both, "/a/x/b.md") == "personal"


# --- the loop ----------------------------------------------------------------


def _fake_record(**kw) -> Record:
    base = dict(
        title="A Title", description="A description.", concept_type="Runbook",
        tags=["alpha"], entities=[{"name": "Acme", "kind": "organization"}],
        sensitivity="work",
    )
    base.update(kw)
    return Record(**base)


def test_run_enrich_populates_concepts(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr("kb.enrich.check_model", lambda c: (True, "test-model"))
    monkeypatch.setattr("kb.enrich.enrich_one", lambda c, f, p, t: _fake_record())

    with Manifest(tmp_path / "m.db") as mf:
        scan(cfg, mf, do_hash=True)
        from kb.extract.runner import run_extract

        run_extract(cfg, mf, show_progress=False)
        stats = run_enrich(cfg, mf, show_progress=False)

        assert stats["ok"] > 0 and stats["failed"] == 0
        rows = mf.conn.execute(
            "SELECT concept_id, tags, entities FROM concepts WHERE enrich_status='ok'"
        ).fetchall()
        assert len(rows) == stats["ok"]
        assert all(r["concept_id"].startswith("runbook/") for r in rows)
        assert json.loads(rows[0]["entities"])[0]["name"] == "Acme"
        # Titles collide by construction, so ids must have been disambiguated.
        assert len({r["concept_id"] for r in rows}) == len(rows)


def test_run_enrich_resumes_and_skips_done_work(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr("kb.enrich.check_model", lambda c: (True, "test-model"))
    monkeypatch.setattr("kb.enrich.enrich_one", lambda c, f, p, t: _fake_record())

    with Manifest(tmp_path / "m.db") as mf:
        scan(cfg, mf, do_hash=True)
        from kb.extract.runner import run_extract

        run_extract(cfg, mf, show_progress=False)

        first = run_enrich(cfg, mf, limit=2, show_progress=False)
        assert first["ok"] == 2
        second = run_enrich(cfg, mf, show_progress=False)
        assert second["ok"] > 0
        third = run_enrich(cfg, mf, show_progress=False)
        assert third["ok"] == 0  # nothing left pending


def test_concept_id_is_frozen_across_reruns(cfg, tmp_path, monkeypatch):
    """A re-run that infers a different type must not move the concept."""
    monkeypatch.setattr("kb.enrich.check_model", lambda c: (True, "test-model"))
    monkeypatch.setattr("kb.enrich.enrich_one", lambda c, f, p, t: _fake_record())

    with Manifest(tmp_path / "m.db") as mf:
        scan(cfg, mf, do_hash=True)
        from kb.extract.runner import run_extract

        run_extract(cfg, mf, show_progress=False)
        run_enrich(cfg, mf, show_progress=False)
        before = dict(
            mf.conn.execute("SELECT source_hash, concept_id FROM concepts").fetchall()
        )

        # Same documents, but now everything classifies as Reference.
        monkeypatch.setattr(
            "kb.enrich.enrich_one",
            lambda c, f, p, t: _fake_record(concept_type="Reference"),
        )
        mf.conn.execute("UPDATE concepts SET enrich_status = 'pending'")
        mf.commit()
        run_enrich(cfg, mf, show_progress=False)

        after = dict(
            mf.conn.execute("SELECT source_hash, concept_id FROM concepts").fetchall()
        )
        assert after == before
        types = {r[0] for r in mf.conn.execute("SELECT concept_type FROM concepts")}
        assert types == {"Reference"}  # type updated, id did not


def test_run_enrich_records_failure_and_continues(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr("kb.enrich.check_model", lambda c: (True, "test-model"))
    calls = {"n": 0}

    def flaky(c, filename, path, text):
        calls["n"] += 1
        if calls["n"] <= 2:  # both attempts on the first document fail
            raise RuntimeError("model exploded")
        return _fake_record()

    monkeypatch.setattr("kb.enrich.enrich_one", flaky)

    with Manifest(tmp_path / "m.db") as mf:
        scan(cfg, mf, do_hash=True)
        from kb.extract.runner import run_extract

        run_extract(cfg, mf, show_progress=False)
        stats = run_enrich(cfg, mf, show_progress=False)

        assert stats["failed"] == 1
        assert stats["ok"] > 0
        bad = mf.conn.execute(
            "SELECT error FROM concepts WHERE enrich_status = 'failed'"
        ).fetchone()
        assert "model exploded" in bad["error"]


def test_run_enrich_refuses_when_model_is_missing(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr("kb.enrich.check_model", lambda c: (False, "not pulled"))
    with Manifest(tmp_path / "m.db") as mf:
        scan(cfg, mf, do_hash=True)
        with pytest.raises(RuntimeError, match="not pulled"):
            run_enrich(cfg, mf, show_progress=False)


def test_enrichable_excludes_missing_sources(cfg, tmp_path, monkeypatch):
    """A file that later falls under a deny glob must not produce a concept."""
    with Manifest(tmp_path / "m.db") as mf:
        scan(cfg, mf, do_hash=True)
        from kb.extract.runner import run_extract

        run_extract(cfg, mf, show_progress=False)
        before = len(mf.enrichable())

        mf.conn.execute(
            "UPDATE files SET scan_status = 'missing' WHERE path LIKE '%note1.md'"
        )
        mf.commit()
        assert len(mf.enrichable()) == before - 1


def test_enrich_stops_on_the_wall_clock_budget(cfg, tmp_path, monkeypatch):
    """A budgeted run stops between documents, never mid-document."""
    monkeypatch.setattr("kb.enrich.check_model", lambda c: (True, "test-model"))

    calls = {"n": 0}

    def slow(c, filename, path, text):
        calls["n"] += 1
        import time as _t

        _t.sleep(0.05)
        return _fake_record()

    monkeypatch.setattr("kb.enrich.enrich_one", slow)

    with Manifest(tmp_path / "m.db") as mf:
        scan(cfg, mf, do_hash=True)
        from kb.extract.runner import run_extract

        run_extract(cfg, mf, show_progress=False)
        total = len(mf.enrichable())
        stats = run_enrich(cfg, mf, max_seconds=0.12, show_progress=False)

        assert stats.get("stopped_on_budget") is True
        assert 0 < stats["ok"] < total          # some done, some left
        assert len(mf.enrichable()) == total - stats["ok"]   # rest still queued


def test_enrich_without_a_budget_finishes_everything(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr("kb.enrich.check_model", lambda c: (True, "test-model"))
    monkeypatch.setattr("kb.enrich.enrich_one", lambda c, f, p, t: _fake_record())
    with Manifest(tmp_path / "m.db") as mf:
        scan(cfg, mf, do_hash=True)
        from kb.extract.runner import run_extract

        run_extract(cfg, mf, show_progress=False)
        stats = run_enrich(cfg, mf, show_progress=False)
        assert "stopped_on_budget" not in stats
        assert mf.enrichable() == []


# --- Entities are things, not files ------------------------------------------


def test_filenames_are_not_entities():
    """The enricher was returning README.md and DEPLOYMENT.md as entities.

    They then became Entity nodes with MENTIONS edges, so "documents that
    mention README.md" linked every repo in the corpus to every other. 351 of
    16,045 mentions on the real corpus.
    """
    for name in ("README.md", "CLAUDE.md", "app.py", "effect-gate.ts",
                 "docker-compose.yml", "config.toml", "notes.txt", "report.pdf"):
        assert is_file_like(name), name


def test_a_path_is_not_an_entity():
    for name in ("src/startup/index.ts", "docs/planning/RUNBOOK.md"):
        assert is_file_like(name), name


def test_a_url_is_not_an_entity():
    for name in ("https://example.com", "http://example.org/x", "www.example.org"):
        assert is_file_like(name), name


def test_technologies_named_like_files_survive():
    """The trap. Next.js and Node.js match every naive filename pattern and are
    real entities. A blunt extension rule would delete 22 real mentions."""
    for name in ("Next.js", "Node.js", "Vue.js", "Express.js"):
        assert not is_file_like(name), name


def test_names_containing_a_slash_survive():
    """BSA/AML and MSP/Datto are real, and a path check alone would eat them."""
    for name in ("BSA/AML", "MSP/Datto", "name/nickname/twitter", "dana-fork/main"):
        assert not is_file_like(name), name


def test_ordinary_entities_are_untouched():
    for name in ("Redwood", "Ada Lovelace", "Splunk", "Redwood Inc.", "PostgreSQL"):
        assert not is_file_like(name), name


def test_empty_and_degenerate_names_do_not_raise():
    for name in ("", "   ", ".", "...", None):
        assert is_file_like(name) in (True, False)
