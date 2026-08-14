"""OKF v0.2 primitives and the conformance validator.

The validator is tested in both directions: every rejection has a matching
acceptance, and the correction each error names is proven to pass. A guard
proven only to reject is indistinguishable from one that rejects everything.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from kb.okf import (
    OKF_VERSION,
    Document,
    IndexEntry,
    allocate_concept_id,
    concept_id_for,
    link_target,
    parse,
    prepend_log_entry,
    render,
    render_index,
    slugify,
    validate_bundle,
    write_atomic,
)

CONCEPT = "---\ntype: Reference\ntitle: A Thing\n---\n\n# Body\n\ntext\n"


# --- frontmatter -------------------------------------------------------------


def test_parse_round_trips():
    doc = parse(CONCEPT)
    assert doc is not None
    assert doc.frontmatter["type"] == "Reference"
    assert "# Body" in doc.body
    again = parse(render(doc))
    assert again is not None
    assert again.frontmatter == doc.frontmatter


def test_parse_rejects_missing_frontmatter():
    assert parse("# Just a heading\n") is None


def test_parse_rejects_broken_yaml():
    assert parse("---\ntype: [unclosed\n---\n\nbody\n") is None


def test_parse_rejects_non_mapping_frontmatter():
    assert parse("---\n- a\n- b\n---\n\nbody\n") is None


def test_parse_accepts_empty_frontmatter_block():
    doc = parse("---\n\n---\n\nbody\n")
    assert doc is not None and doc.frontmatter == {}
    assert doc.type is None


def test_type_property_treats_blank_as_missing():
    assert parse("---\ntype: '  '\n---\n\nb\n").type is None


def test_render_keeps_nested_structures():
    doc = Document(
        frontmatter={
            "type": "Metric",
            "entities": [{"name": "Acme", "kind": "organization"}],
            "generated": {"by": "okf-kb/x", "at": "2026-08-07T00:00:00Z"},
        },
        body="# Body",
    )
    back = parse(render(doc))
    assert back is not None
    assert back.frontmatter["entities"][0]["name"] == "Acme"
    assert back.frontmatter["generated"]["by"] == "okf-kb/x"


# --- ids and slugs -----------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Hello World", "hello-world"),
        ("  Mixed_Case/Slashes  ", "mixed-case-slashes"),
        ("!!!", "untitled"),
        ("Café Naïve", "caf-na-ve"),
    ],
)
def test_slugify(text, expected):
    assert slugify(text) == expected


def test_concept_id_is_path_minus_md(tmp_path):
    b = tmp_path / "bundle"
    p = b / "runbook" / "thing.md"
    assert concept_id_for(p, b) == "runbook/thing"


def test_link_target_is_bundle_relative():
    assert link_target("runbook/thing") == "/runbook/thing.md"


def test_allocate_concept_id_is_stable_and_collision_free():
    taken: set[str] = set()
    a = allocate_concept_id("Meeting Notes", "Weekly Sync", "aaaa1111", taken)
    taken.add(a)
    b = allocate_concept_id("Meeting Notes", "Weekly Sync", "bbbb2222", taken)
    assert a == "meeting-notes/weekly-sync"
    assert b.startswith("meeting-notes/weekly-sync-") and b != a
    # Same hash, same set state -> same id every run.
    assert allocate_concept_id("Meeting Notes", "Weekly Sync", "bbbb2222", {a}) == b


def test_allocate_concept_id_avoids_reserved_filenames():
    for reserved in ("index", "log"):
        got = allocate_concept_id("Reference", reserved, "abc123", set())
        assert not got.endswith("/index") and not got.endswith("/log")


# --- index and log -----------------------------------------------------------


def test_render_index_has_no_frontmatter_by_default():
    out = render_index({"Group": [IndexEntry("T", "t.md", "desc")]})
    assert not out.startswith("---")
    assert "* [T](t.md) - desc" in out


def test_render_index_root_carries_okf_version():
    out = render_index({"G": [IndexEntry("T", "t.md")]}, okf_version=OKF_VERSION)
    assert out.startswith(f'---\nokf_version: "{OKF_VERSION}"\n---')


def test_log_entries_are_newest_first(tmp_path):
    p = tmp_path / "log.md"
    prepend_log_entry(p, date(2026, 1, 1), ["* **Creation**: first"], "Log")
    prepend_log_entry(p, date(2026, 3, 5), ["* **Update**: second"], "Log")
    text = p.read_text()
    assert text.index("## 2026-03-05") < text.index("## 2026-01-01")


def test_log_same_day_merges_under_one_heading(tmp_path):
    p = tmp_path / "log.md"
    prepend_log_entry(p, date(2026, 3, 5), ["* **Update**: a"], "Log")
    prepend_log_entry(p, date(2026, 3, 5), ["* **Update**: b"], "Log")
    text = p.read_text()
    assert text.count("## 2026-03-05") == 1
    assert text.index("* **Update**: b") < text.index("* **Update**: a")


def test_write_atomic_leaves_no_temp_files(tmp_path):
    p = tmp_path / "a" / "b.md"
    write_atomic(p, "hello")
    assert p.read_text() == "hello"
    assert list(tmp_path.rglob("*.tmp")) == []


# --- validator: the conformant baseline --------------------------------------


@pytest.fixture
def good_bundle(tmp_path) -> Path:
    b = tmp_path / "bundle"
    (b / "runbook").mkdir(parents=True)
    (b / "runbook" / "restart.md").write_text(
        "---\ntype: Runbook\ntitle: Restart\ndescription: How to restart.\n---\n\n"
        "# Steps\n\nSee [other](/runbook/other.md).\n"
    )
    (b / "runbook" / "other.md").write_text(
        "---\ntype: Runbook\ntitle: Other\ndescription: Another.\n---\n\n# Steps\n"
    )
    (b / "runbook" / "index.md").write_text("# Runbook\n\n* [Restart](restart.md)\n")
    (b / "index.md").write_text(f'---\nokf_version: "{OKF_VERSION}"\n---\n\n# All\n')
    (b / "log.md").write_text("# Log\n\n## 2026-08-07\n* **Creation**: made it.\n")
    return b


def test_valid_bundle_passes(good_bundle):
    rep = validate_bundle(good_bundle)
    assert rep.ok, [f.message for f in rep.errors]
    assert rep.errors == []
    assert rep.concepts == 2
    assert rep.indexes == 2
    assert rep.logs == 1
    assert rep.broken_links == 0


def test_validator_counts_internal_links(good_bundle):
    assert validate_bundle(good_bundle).links >= 2


# --- validator: each rejection, and the correction it names -------------------


def test_missing_frontmatter_is_an_error_and_adding_it_fixes(good_bundle):
    bad = good_bundle / "runbook" / "loose.md"
    bad.write_text("# No frontmatter here\n")
    rep = validate_bundle(good_bundle)
    assert not rep.ok
    assert any("no parseable YAML frontmatter" in f.message for f in rep.errors)

    bad.write_text("---\ntype: Runbook\n---\n\n# Now it has one\n")
    assert validate_bundle(good_bundle).ok


def test_missing_type_is_an_error_and_adding_type_fixes(good_bundle):
    bad = good_bundle / "runbook" / "typeless.md"
    bad.write_text("---\ntitle: No type\n---\n\n# Body\n")
    rep = validate_bundle(good_bundle)
    assert not rep.ok
    assert any("no non-empty `type`" in f.message for f in rep.errors)

    bad.write_text("---\ntype: Runbook\ntitle: No type\n---\n\n# Body\n")
    assert validate_bundle(good_bundle).ok


def test_empty_type_string_is_an_error(good_bundle):
    (good_bundle / "runbook" / "blank.md").write_text("---\ntype: ''\n---\n\n# B\n")
    assert not validate_bundle(good_bundle).ok


def test_frontmatter_in_a_subdirectory_index_is_an_error(good_bundle):
    idx = good_bundle / "runbook" / "index.md"
    idx.write_text("---\ntype: Index\n---\n\n# Runbook\n")
    rep = validate_bundle(good_bundle)
    assert not rep.ok
    assert any("index.md must not contain frontmatter" in f.message for f in rep.errors)

    idx.write_text("# Runbook\n\n* [Restart](restart.md)\n")
    assert validate_bundle(good_bundle).ok


def test_root_index_may_only_carry_okf_version(good_bundle):
    root = good_bundle / "index.md"
    root.write_text('---\nokf_version: "0.2"\ntype: Thing\n---\n\n# All\n')
    rep = validate_bundle(good_bundle)
    assert not rep.ok
    assert any("only hold okf_version" in f.message for f in rep.errors)

    root.write_text('---\nokf_version: "0.2"\n---\n\n# All\n')
    assert validate_bundle(good_bundle).ok


def test_root_index_with_no_frontmatter_is_fine(good_bundle):
    (good_bundle / "index.md").write_text("# All\n\n* [Runbook](runbook/)\n")
    assert validate_bundle(good_bundle).ok


def test_bad_log_heading_is_an_error_and_the_iso_form_fixes(good_bundle):
    logf = good_bundle / "log.md"
    logf.write_text("# Log\n\n## August 7 2026\n* **Update**: nope.\n")
    rep = validate_bundle(good_bundle)
    assert not rep.ok
    assert any("not `## YYYY-MM-DD`" in f.message for f in rep.errors)

    logf.write_text("# Log\n\n## 2026-08-07\n* **Update**: yes.\n")
    assert validate_bundle(good_bundle).ok


def test_missing_bundle_directory_is_an_error(tmp_path):
    rep = validate_bundle(tmp_path / "nope")
    assert not rep.ok


# --- validator: what the spec forbids rejecting ------------------------------


def test_broken_links_are_warnings_not_errors(good_bundle):
    (good_bundle / "runbook" / "restart.md").write_text(
        "---\ntype: Runbook\ntitle: R\ndescription: d\n---\n\n"
        "See [gone](/runbook/does-not-exist.md).\n"
    )
    rep = validate_bundle(good_bundle)
    assert rep.ok  # §6.1: consumers MUST tolerate broken links
    assert rep.broken_links == 1
    assert any("broken link" in f.message for f in rep.warnings)


def test_unknown_type_and_extra_keys_are_accepted(good_bundle):
    (good_bundle / "runbook" / "weird.md").write_text(
        "---\ntype: Totally Invented Type\ntitle: W\ndescription: d\n"
        "some_custom_key: 42\n---\n\n# Body\n"
    )
    assert validate_bundle(good_bundle).ok


def test_missing_recommended_keys_are_warnings_only(good_bundle):
    (good_bundle / "runbook" / "bare.md").write_text("---\ntype: Runbook\n---\n\n# B\n")
    rep = validate_bundle(good_bundle)
    assert rep.ok
    assert any("missing recommended key" in f.message for f in rep.warnings)


def test_missing_root_index_is_a_warning_only(good_bundle):
    (good_bundle / "index.md").unlink()
    rep = validate_bundle(good_bundle)
    assert rep.ok
    assert any("no index.md" in f.message for f in rep.warnings)


def test_entity_review_is_not_validated_as_a_concept(good_bundle):
    """8C writes bundle/entity-review.md, a report with no frontmatter.

    Without this, the 02:00 drain runs bundle-then-validate and fails on a file
    the build itself just wrote.
    """
    (good_bundle / "entity-review.md").write_text("# Entity review\n\nNo YAML.\n")
    rep = validate_bundle(good_bundle)
    assert rep.ok, [f.message for f in rep.errors]


def test_external_links_are_not_link_checked(good_bundle):
    (good_bundle / "runbook" / "restart.md").write_text(
        "---\ntype: Runbook\ntitle: R\ndescription: d\n---\n\n"
        "[web](https://example.com/x) and [mail](mailto:a@b.c)\n"
    )
    rep = validate_bundle(good_bundle)
    assert rep.ok and rep.broken_links == 0


# --- mutation check ----------------------------------------------------------


def test_neutered_validator_would_be_caught(good_bundle, monkeypatch):
    """If the type check stopped firing, the suite must notice.

    Proves the rejection tests above depend on the guard rather than on some
    incidental property of the fixtures.
    """
    (good_bundle / "runbook" / "typeless.md").write_text(
        "---\ntitle: No type\n---\n\n# Body\n"
    )
    assert not validate_bundle(good_bundle).ok

    import kb.okf as okf

    monkeypatch.setattr(okf.Document, "type", property(lambda self: "forced"))
    assert validate_bundle(good_bundle).ok  # guard neutered, bad bundle now passes
