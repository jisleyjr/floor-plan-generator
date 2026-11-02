"""Keep service configuration tests independent of the developer environment."""
from __future__ import annotations

import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

_SERVICE_ENV = ("DATABASE_URL", "FLOORPLAN_DATA_DIR", "FLOORPLAN_API_KEY")
_ROOT = Path(__file__).resolve().parents[1]
_COMPOSE_FILE = _ROOT / "tests" / "docker-compose.databases.yml"
_COMPOSE_PROJECT = "floorplan-db-tests"
_DB_PASSWORD = "floorplan-test"


@pytest.fixture(autouse=True)
def _clear_service_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _SERVICE_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def database_servers() -> dict[str, int]:
    """Start disposable MySQL 8.4 and MariaDB 11.4, then remove them.

    The application never calls this. Only tests that opt into those backends
    do, via ``backend_url``.
    """
    base = ["docker", "compose", "-f", str(_COMPOSE_FILE), "-p", _COMPOSE_PROJECT]
    up = subprocess.run([*base, "up", "-d", "--wait"], capture_output=True, text=True, timeout=180)
    if up.returncode != 0:
        subprocess.run([*base, "down", "-v"], capture_output=True, text=True)
        raise RuntimeError(f"Could not start database containers.\n{up.stdout}\n{up.stderr}")
    try:
        ports: dict[str, int] = {}
        for service in ("mysql", "mariadb"):
            listed = subprocess.run(
                [*base, "port", service, "3306"],
                check=True,
                capture_output=True,
                text=True,
            )
            ports[service] = int(listed.stdout.strip().rsplit(":", 1)[1])
        yield ports
    finally:
        subprocess.run([*base, "down", "-v"], capture_output=True, text=True, timeout=120)


@pytest.fixture(params=["sqlite", "mysql", "mariadb"])
def backend_url(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    """A migrated-ready empty database URL for one backend."""
    url, drop = _provision_database(request.param, tmp_path, request)
    try:
        yield url
    finally:
        drop()


def _provision_database(
    backend: str, tmp_path: Path, request: pytest.FixtureRequest
) -> tuple[str, Callable[[], None]]:
    if backend == "sqlite":
        return "sqlite:///" + (tmp_path / "queue.sqlite").as_posix(), lambda: None
    if backend not in {"mysql", "mariadb"}:
        raise ValueError(f"Unknown database backend {backend!r}.")
    servers = request.getfixturevalue("database_servers")
    port = servers[backend]
    name = "fp_" + uuid.uuid4().hex
    admin_url = f"mysql+pymysql://root:{_DB_PASSWORD}@127.0.0.1:{port}/?charset=utf8mb4"
    _statement(admin_url, f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    url = f"mysql+pymysql://root:{_DB_PASSWORD}@127.0.0.1:{port}/{name}?charset=utf8mb4"

    def drop() -> None:
        _statement(admin_url, f"DROP DATABASE IF EXISTS `{name}`")

    return url, drop


def _statement(url: str, statement: str) -> None:
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(text(statement))
    finally:
        engine.dispose()
