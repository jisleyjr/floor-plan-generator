"""Process configuration shared by the API, worker, and migrations.

Values come from the environment. A blank variable is treated as unset.
This module does not read ``.env`` files and does not create directories.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_SQLITE_NAME = "floorplan.sqlite"


class ConfigurationError(RuntimeError):
    """Required service configuration is missing or unusable."""


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_url: str
    api_key: str | None

    def require_api_key(self) -> str:
        if not self.api_key:
            raise ConfigurationError(
                "FLOORPLAN_API_KEY is required to start the API. "
                "Set FLOORPLAN_API_KEY in the environment (see .env.example) and start again."
            )
        return self.api_key


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _blank_as_missing(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _data_dir_from_env(root: Path) -> Path:
    raw = _blank_as_missing(os.environ.get("FLOORPLAN_DATA_DIR"))
    if raw is None:
        return (root / ".runtime").resolve()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _database_url(data_dir: Path) -> str:
    raw = _blank_as_missing(os.environ.get("DATABASE_URL"))
    if raw is not None:
        return raw
    database = (data_dir / _SQLITE_NAME).resolve()
    return "sqlite:///" + database.as_posix()


def get_settings() -> Settings:
    """Resolve the current environment. Callers that need a fresh value call this again."""
    root = repository_root()
    data_dir = _data_dir_from_env(root)
    return Settings(
        data_dir=data_dir,
        database_url=_database_url(data_dir),
        api_key=_blank_as_missing(os.environ.get("FLOORPLAN_API_KEY")),
    )
