"""Configuration resolved from the environment."""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_default_data_dir_is_gitignored_runtime_under_the_repo() -> None:
    from fpgen.settings import get_settings

    settings = get_settings()

    assert settings.data_dir == (ROOT / ".runtime").resolve()
    assert settings.data_dir.is_absolute()


def test_default_database_url_is_absolute_sqlite_inside_the_runtime_dir() -> None:
    from fpgen.settings import get_settings

    settings = get_settings()
    database = (ROOT / ".runtime" / "floorplan.sqlite").resolve()

    assert settings.database_url == "sqlite:///" + database.as_posix()
    assert settings.database_url.startswith("sqlite:////")


def test_relative_data_dir_resolves_from_the_repository_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLOORPLAN_DATA_DIR", "var/floorplan-data")
    from fpgen.settings import get_settings

    settings = get_settings()

    assert settings.data_dir == (ROOT / "var" / "floorplan-data").resolve()
    assert settings.database_url == "sqlite:///" + (settings.data_dir / "floorplan.sqlite").as_posix()


def test_absolute_data_dir_is_used_as_given(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FLOORPLAN_DATA_DIR", str(tmp_path / "runtime"))
    from fpgen.settings import get_settings

    assert get_settings().data_dir == (tmp_path / "runtime").resolve()


def test_database_url_override_wins_over_the_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FLOORPLAN_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://floorplan:floorplan@127.0.0.1:3306/floorplan")
    from fpgen.settings import get_settings

    assert get_settings().database_url == "mysql+pymysql://floorplan:floorplan@127.0.0.1:3306/floorplan"


def test_blank_database_url_and_data_dir_fall_back_to_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "  ")
    monkeypatch.setenv("FLOORPLAN_DATA_DIR", "")
    from fpgen.settings import get_settings

    settings = get_settings()

    assert settings.data_dir == (ROOT / ".runtime").resolve()
    assert settings.database_url.endswith("/.runtime/floorplan.sqlite")


def test_reading_settings_does_not_create_the_data_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "runtime"
    monkeypatch.setenv("FLOORPLAN_DATA_DIR", str(data_dir))
    from fpgen.settings import get_settings

    get_settings()

    assert not data_dir.exists()


def test_api_key_is_optional_for_settings_and_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    from fpgen.settings import ConfigurationError, get_settings

    assert get_settings().api_key is None
    with pytest.raises(ConfigurationError, match="FLOORPLAN_API_KEY"):
        get_settings().require_api_key()

    monkeypatch.setenv("FLOORPLAN_API_KEY", "  test-key  ")
    settings = get_settings()
    assert settings.api_key == "test-key"
    assert settings.require_api_key() == "test-key"


def test_whitespace_api_key_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLOORPLAN_API_KEY", "   ")
    from fpgen.settings import ConfigurationError, get_settings

    assert get_settings().api_key is None
    with pytest.raises(ConfigurationError, match="FLOORPLAN_API_KEY"):
        get_settings().require_api_key()
