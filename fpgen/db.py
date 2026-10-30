"""Database engine and session helpers.

SQLite connection settings live here. Queue operations must not emit
SQLite-specific SQL. Importing this module does not connect or create tables.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from fpgen.settings import Settings, get_settings


def sqlite_file_path(url: str) -> Path | None:
    parsed = make_url(url)
    if not parsed.drivername.startswith("sqlite"):
        return None
    database = parsed.database
    if not database or database == ":memory:":
        return None
    return Path(database)


def ensure_sqlite_parent(url: str) -> None:
    path = sqlite_file_path(url)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)


def connect_args(url: str) -> dict[str, object]:
    parsed = make_url(url)
    if parsed.drivername.startswith("sqlite"):
        return {"check_same_thread": False}
    return {}


def get_engine(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    ensure_sqlite_parent(settings.database_url)
    return create_engine(settings.database_url, connect_args=connect_args(settings.database_url))


def create_migration_engine(settings: Settings | None = None) -> Engine:
    """Engine used by Alembic. Same URL and SQLite arguments as :func:`get_engine`."""
    settings = settings or get_settings()
    ensure_sqlite_parent(settings.database_url)
    return create_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args=connect_args(settings.database_url),
    )


def get_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(settings), autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    """Short-lived session. The caller commits. The engine is closed afterwards."""
    session = get_session_factory(settings)()
    engine = session.get_bind()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
