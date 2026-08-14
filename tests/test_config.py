"""config.toml parsing, and a smoke pass over the CLI."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kb.cli import app
from kb.config import load_config

CONFIG = """\
[scan]
max_file_bytes = 2048
follow_symlinks = true
same_device_only = false
include_source_code = {code}

[[scan.roots]]
path = "~/Documents"
enabled = true
sensitivity = "personal"

[[scan.roots]]
path = "$HOME/agents"
enabled = false
sensitivity = "work"

[scan.deny]
globs = ["node_modules", "*.dmg"]

[extract]
out_dir = "data/extracted"
min_chars = 7

[extract.ocr]
enabled = false
chars_per_page_threshold = 55
scanned_page_fraction = 0.4
sample_pages = 3
max_pages = 9
dpi = 150
images = true

[logging]
level = "debug"
format = "json"
"""


def write_config(tmp_path: Path, code: bool = False) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(CONFIG.format(code=str(code).lower()), encoding="utf-8")
    return p


def test_load_config_round_trips_scan_settings(tmp_path):
    cfg = load_config(write_config(tmp_path))
    assert cfg.max_file_bytes == 2048
    assert cfg.follow_symlinks is True
    assert cfg.same_device_only is False
    assert cfg.deny_globs == ["node_modules", "*.dmg"]


@pytest.mark.parametrize("code", [True, False])
def test_include_source_code_round_trips(tmp_path, code):
    assert load_config(write_config(tmp_path, code=code)).include_source_code is code


def test_include_source_code_defaults_to_false(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[scan]\n", encoding="utf-8")
    assert load_config(p).include_source_code is False


def test_roots_expand_tilde_and_env(tmp_path):
    cfg = load_config(write_config(tmp_path))
    paths = [str(r.path) for r in cfg.roots]
    assert all(p.startswith("/") for p in paths)
    assert not any("~" in p or "$" in p for p in paths)


def test_enabled_roots_filters_disabled(tmp_path):
    cfg = load_config(write_config(tmp_path))
    assert len(cfg.roots) == 2
    enabled = cfg.enabled_roots()
    assert len(enabled) == 1
    assert enabled[0].sensitivity == "personal"


def test_ocr_block_round_trips(tmp_path):
    ocr = load_config(write_config(tmp_path)).ocr
    assert ocr.enabled is False
    assert ocr.chars_per_page_threshold == 55
    assert ocr.scanned_page_fraction == 0.4
    assert ocr.sample_pages == 3
    assert ocr.max_pages == 9
    assert ocr.dpi == 150
    assert ocr.images is True


def test_derived_paths_are_relative_to_the_config(tmp_path):
    cfg = load_config(write_config(tmp_path))
    assert cfg.root_dir == tmp_path
    assert cfg.extract_out_dir == tmp_path / "data" / "extracted"
    assert cfg.manifest_path == tmp_path / "data" / "manifest.db"
    assert cfg.bundle_dir == tmp_path / "bundle"


def test_logging_settings_normalised(tmp_path):
    cfg = load_config(write_config(tmp_path))
    assert cfg.log_level == "DEBUG"
    assert cfg.log_format == "json"


def test_missing_config_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.toml")


def test_shipped_config_parses():
    """The real config.toml in the repo must stay loadable."""
    from kb.config import repo_root

    cfg = load_config(repo_root() / "config.toml")
    assert cfg.enabled_roots()
    assert cfg.deny_globs


# --- CLI ---------------------------------------------------------------------


def test_cli_doctor_runs(tmp_path):
    result = CliRunner().invoke(app, ["doctor", "--config", str(write_config(tmp_path))])
    assert result.exit_code == 0, result.output
    assert "root" in result.output


def test_cli_report_on_empty_manifest(tmp_path):
    result = CliRunner().invoke(app, ["report", "--config", str(write_config(tmp_path))])
    assert result.exit_code == 0, result.output
    assert "Scan status" in result.output


def test_cli_scan_and_extract_end_to_end(tmp_path, corpus):
    p = tmp_path / "config.toml"
    p.write_text(
        textwrap.dedent(f"""\
            [scan]
            max_file_bytes = 65536
            include_source_code = false

            [[scan.roots]]
            path = "{corpus}"
            enabled = true

            [scan.deny]
            globs = ["node_modules", "*.zip"]

            [extract]
            min_chars = 5

            [extract.ocr]
            enabled = false
            """),
        encoding="utf-8",
    )
    runner = CliRunner()
    scan = runner.invoke(app, ["scan", "--config", str(p)])
    assert scan.exit_code == 0, scan.output

    extract = runner.invoke(app, ["extract", "--config", str(p)])
    assert extract.exit_code == 0, extract.output
    assert "Total extracted words" in extract.output
    assert (tmp_path / "data" / "manifest.db").exists()
    assert list((tmp_path / "data" / "extracted").rglob("*.md"))
