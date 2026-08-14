from __future__ import annotations

from pathlib import Path

import pytest

from kb.manifest import DenyList, Manifest, StatRow, scan, sniff_mime


# --- denylist ----------------------------------------------------------------


@pytest.mark.parametrize(
    "path,is_dir,expected",
    [
        ("/Users/x/node_modules", True, "node_modules"),
        ("/Users/x/proj/.git", True, ".git"),
        ("/Users/x/a.dmg", False, "*.dmg"),
        ("/Users/x/repo.git", True, "*.git"),
        ("/Users/x/Library", True, "*/library"),
        ("/Users/x/notes.md", False, None),
        ("/Users/x/src/app.py", False, None),
    ],
)
def test_denylist_matches(path, is_dir, expected):
    deny = DenyList(["node_modules", ".git", "*.git", "*.dmg", "*/Library"])
    assert deny.match(Path(path), is_dir=is_dir) == expected


def test_denylist_prunes_files_under_denied_component():
    deny = DenyList(["node_modules"])
    assert deny.match(Path("/a/node_modules/pkg/index.js"), is_dir=False) == "node_modules"


# --- mime --------------------------------------------------------------------


def test_sniff_mime_prefers_extension(tmp_path):
    p = tmp_path / "x.md"
    p.write_text("hi")
    assert sniff_mime(p, ".md") == "text/markdown"


def test_sniff_mime_falls_back_to_magic(tmp_path):
    p = tmp_path / "noext"
    p.write_bytes(b"%PDF-1.7\nrest")
    assert sniff_mime(p, "") == "application/pdf"


def test_sniff_mime_detects_text_without_extension(tmp_path):
    p = tmp_path / "Makefile"
    p.write_bytes(b"all:\n\techo hi\n")
    assert sniff_mime(p, "") == "text/plain"


def test_sniff_mime_ooxml_refines_zip(tmp_path):
    p = tmp_path / "x.unknownext"
    p.write_bytes(b"PK\x03\x04" + b"\x00" * 16)
    assert sniff_mime(p, ".docx").endswith("wordprocessingml.document")


# --- manifest ----------------------------------------------------------------


def _row(path: str, size: int = 10, mtime: float = 1.0, inode: int = 1) -> StatRow:
    return StatRow(
        path=path, root="/root", size=size, mtime=mtime, inode=inode,
        ext=".md", mime="text/markdown", scan_status="included",
    )


def test_upsert_preserves_hash_when_stat_unchanged(tmp_path):
    with Manifest(tmp_path / "m.db") as mf:
        run = mf.start_run("test")
        mf.upsert_stat(_row("/a.md"), run)
        mf.commit()
        mf.set_hash("/a.md", "deadbeef")
        mf.set_extract_result("deadbeef", "ok", "/out.md", "plain", 5, None)
        mf.commit()

        mf.upsert_stat(_row("/a.md"), mf.start_run("test2"))
        mf.commit()
        got = mf.conn.execute("SELECT * FROM files WHERE path='/a.md'").fetchone()
        assert got["hash"] == "deadbeef"
        assert got["extract_status"] == "ok"


def test_upsert_clears_hash_when_mtime_changes(tmp_path):
    with Manifest(tmp_path / "m.db") as mf:
        run = mf.start_run("test")
        mf.upsert_stat(_row("/a.md"), run)
        mf.set_hash("/a.md", "deadbeef")
        mf.set_extract_result("deadbeef", "ok", "/out.md", "plain", 5, None)
        mf.commit()

        mf.upsert_stat(_row("/a.md", mtime=2.0), mf.start_run("test2"))
        mf.commit()
        got = mf.conn.execute("SELECT * FROM files WHERE path='/a.md'").fetchone()
        assert got["hash"] is None
        assert got["extract_status"] == "pending"


def test_needs_hash_is_resumable(tmp_path):
    with Manifest(tmp_path / "m.db") as mf:
        run = mf.start_run("test")
        mf.upsert_stat(_row("/a.md"), run)
        mf.upsert_stat(_row("/b.md", inode=2), run)
        mf.commit()
        assert len(mf.needs_hash()) == 2
        mf.set_hash("/a.md", "aa")
        mf.commit()
        assert [r["path"] for r in mf.needs_hash()] == ["/b.md"]


def test_pending_extractions_deduplicates_by_hash(tmp_path):
    with Manifest(tmp_path / "m.db") as mf:
        run = mf.start_run("test")
        mf.upsert_stat(_row("/a.md"), run)
        mf.upsert_stat(_row("/b.md", inode=2), run)
        mf.commit()
        mf.set_hash("/a.md", "same")
        mf.set_hash("/b.md", "same")
        mf.commit()
        pending = mf.pending_extractions()
        assert len(pending) == 1
        assert pending[0]["copies"] == 2


def test_extract_result_applies_to_every_copy(tmp_path):
    with Manifest(tmp_path / "m.db") as mf:
        run = mf.start_run("test")
        mf.upsert_stat(_row("/a.md"), run)
        mf.upsert_stat(_row("/b.md", inode=2), run)
        mf.set_hash("/a.md", "same")
        mf.set_hash("/b.md", "same")
        mf.set_extract_result("same", "ok", "/out.md", "plain", 7, None)
        mf.commit()
        rows = mf.conn.execute("SELECT extract_status, word_count FROM files").fetchall()
        assert all(r["extract_status"] == "ok" and r["word_count"] == 7 for r in rows)


def test_mark_missing_flags_unseen_rows(tmp_path):
    with Manifest(tmp_path / "m.db") as mf:
        old = mf.start_run("t1")
        mf.upsert_stat(_row("/root/gone.md"), old)
        mf.commit()
        new = mf.start_run("t2")
        mf.upsert_stat(_row("/root/here.md", inode=2), new)
        mf.commit()
        assert mf.mark_missing(new, ["/root"]) == 1
        got = mf.conn.execute(
            "SELECT scan_status FROM files WHERE path='/root/gone.md'"
        ).fetchone()
        assert got["scan_status"] == "missing"


# --- crawler -----------------------------------------------------------------


def test_scan_classifies_the_fixture_tree(cfg, tmp_path):
    with Manifest(tmp_path / "m.db") as mf:
        stats = scan(cfg, mf, do_hash=True)

        assert stats["included"] > 0
        assert stats["hash_failed"] == 0

        by_status = dict(
            mf.conn.execute(
                "SELECT scan_status, COUNT(*) FROM files GROUP BY scan_status"
            ).fetchall()
        )
        assert by_status.get("too_large") == 1          # big.md
        assert by_status.get("denied", 0) >= 1          # archive.zip
        assert by_status.get("no_route", 0) >= 2        # image.png, code/app.py

        paths = [r[0] for r in mf.conn.execute("SELECT path FROM files").fetchall()]
        assert not any("node_modules" in p for p in paths)

        # dup_a.md and dup_b.md are byte-identical
        dupes = mf.conn.execute(
            "SELECT hash, COUNT(*) n FROM files WHERE hash IS NOT NULL "
            "GROUP BY hash HAVING n > 1"
        ).fetchall()
        assert len(dupes) == 1 and dupes[0]["n"] == 2


def test_scan_is_idempotent(cfg, tmp_path):
    with Manifest(tmp_path / "m.db") as mf:
        first = scan(cfg, mf, do_hash=True)
        second = scan(cfg, mf, do_hash=True)
        assert second["included"] == first["included"]
        # Nothing changed on disk, so the second pass hashes nothing.
        assert second["hashed"] == 0
