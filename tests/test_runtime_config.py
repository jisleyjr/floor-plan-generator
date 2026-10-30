"""API, worker, and migrations share one resolved configuration."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from sqlalchemy import inspect, text

ROOT = Path(__file__).resolve().parents[1]


def _env(**overrides: str) -> dict[str, str]:
    env = os.environ.copy()
    for name in ("DATABASE_URL", "FLOORPLAN_DATA_DIR", "FLOORPLAN_API_KEY"):
        env.pop(name, None)
    env.update(overrides)
    return env


def test_engine_and_session_use_the_resolved_sqlite_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    database = tmp_path / "runtime" / "floorplan.sqlite"
    monkeypatch.setenv("FLOORPLAN_DATA_DIR", str(tmp_path / "runtime"))
    from fpgen.db import get_engine, session_scope
    from fpgen.settings import get_settings

    settings = get_settings()
    engine = get_engine(settings)
    try:
        assert engine.url.database == database.as_posix()
        assert database.parent.is_dir()
        assert inspect(engine).get_table_names() == []
        assert database.is_file()
        with session_scope(settings) as session:
            assert session.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        engine.dispose()


def test_migration_engine_uses_the_same_database_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    database = tmp_path / "migrated.sqlite"
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + database.as_posix())
    from fpgen.db import create_migration_engine
    from fpgen.settings import get_settings

    engine = create_migration_engine()
    try:
        assert engine.url.database == get_settings().database_url.removeprefix("sqlite:///")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        engine.dispose()


def test_api_worker_and_settings_resolve_the_same_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    database = data_dir / "app.sqlite"
    monkeypatch.setenv("FLOORPLAN_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + database.as_posix())
    monkeypatch.setenv("FLOORPLAN_API_KEY", "test-key")
    from fpgen.api import create_app
    from fpgen.settings import get_settings
    from fpgen.worker import load_runtime

    settings = get_settings()
    app = create_app()
    try:
        assert load_runtime() == settings
        assert app.state.settings == settings
        assert app.state.engine.url.database == database.as_posix()
        assert inspect(app.state.engine).get_table_names() == []
    finally:
        app.state.engine.dispose()


def test_create_app_rejects_a_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLOORPLAN_API_KEY", "present-for-import")
    from fpgen.api import create_app
    from fpgen.settings import ConfigurationError

    monkeypatch.delenv("FLOORPLAN_API_KEY", raising=False)
    with pytest.raises(ConfigurationError, match="FLOORPLAN_API_KEY"):
        create_app()


def test_importing_the_api_without_a_key_exits_with_a_clear_error(tmp_path: Path) -> None:
    data_dir = tmp_path / "runtime"
    result = subprocess.run(
        [sys.executable, "-c", "import fpgen.api"],
        cwd=ROOT,
        env=_env(FLOORPLAN_DATA_DIR=str(data_dir)),
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "ERROR:" in result.stderr
    assert "FLOORPLAN_API_KEY" in result.stderr
    assert "Traceback" not in result.stderr
    assert not data_dir.exists()


def test_health_is_reachable_without_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FLOORPLAN_API_KEY", "test-key")
    monkeypatch.setenv("FLOORPLAN_DATA_DIR", str(tmp_path / "runtime"))
    from fpgen.api import create_app

    app = create_app()
    try:
        schema = app.openapi()
        health = schema["paths"]["/health"]["get"]
        assert "security" not in health
        assert not schema.get("security")
        transport = httpx.ASGITransport(app=app)

        async def _get_health() -> httpx.Response:
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.get("/health")

        response = asyncio.run(_get_health())
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
    finally:
        app.state.engine.dispose()


def test_worker_starts_without_an_api_key(tmp_path: Path) -> None:
    data_dir = tmp_path / "runtime"
    result = subprocess.run(
        [sys.executable, "-m", "fpgen.worker"],
        cwd=ROOT,
        env=_env(FLOORPLAN_DATA_DIR=str(data_dir)),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (data_dir / "floorplan.sqlite").is_file()


def test_alembic_upgrade_uses_database_url(tmp_path: Path) -> None:
    database = tmp_path / "from-alembic.sqlite"
    placeholder = Path("/tmp/floorplan-alembic-placeholder-do-not-use.sqlite")
    if placeholder.exists():
        placeholder.unlink()

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=_env(DATABASE_URL="sqlite:///" + database.as_posix()),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert database.is_file()
    assert not placeholder.exists()
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "alembic_version" in tables
    assert "jobs" not in tables


def test_skeleton_modules_import_without_tables() -> None:
    import fpgen.jobs
    import fpgen.models

    assert fpgen.jobs.__doc__
    assert fpgen.models.Base.metadata.tables == {}


def test_floorplan_library_import_does_not_require_an_api_key() -> None:
    code = (
        "from fpgen import generate_layout, validate_layout, load_layout, render_layout, ValidationReport; "
        "assert all((generate_layout, validate_layout, load_layout, render_layout, ValidationReport))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=_env(),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_dependency_files_split_runtime_optional_and_dev() -> None:
    api = (ROOT / "requirements-api.txt").read_text(encoding="utf-8").lower()
    mysql = (ROOT / "requirements-mysql.txt").read_text(encoding="utf-8").lower()
    dev = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").lower()
    runtime = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()

    assert "matplotlib" in runtime
    assert "-r requirements.txt" in api
    assert "fastapi" in api
    assert "uvicorn" in api
    assert "sqlalchemy" in api
    assert "alembic" in api
    assert "pytest" not in api
    assert "pymysql" not in api
    assert "pymysql" in mysql
    assert "pytest" in dev
    assert "httpx" in dev


def test_secrets_runtime_and_test_artifacts_are_ignored() -> None:
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")

    for entry in (".env", ".runtime/", ".pytest_cache/"):
        assert entry in ignore
    assert "!.env.example" in ignore
    for name in ("DATABASE_URL", "FLOORPLAN_DATA_DIR", "FLOORPLAN_API_KEY"):
        assert name in example
    assert "replace-with-a-long-random-string" in example
    assert "sk-" not in example
