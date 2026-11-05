"""Smoke test that the tmp_openvarak_home fixture works."""

from __future__ import annotations

from pathlib import Path

from openvarak.core import config as config_mod


def test_fixture_redirects_default_config_dir(tmp_openvarak_home: Path) -> None:
    assert config_mod.DEFAULT_CONFIG_DIR == tmp_openvarak_home
    assert tmp_openvarak_home.exists()
    assert (tmp_openvarak_home / ".state").exists()
    assert (tmp_openvarak_home / ".state" / "models").exists()


def test_fixture_redirects_config_path(tmp_openvarak_home: Path) -> None:
    assert config_mod.DEFAULT_CONFIG_PATH == tmp_openvarak_home / "config.toml"
