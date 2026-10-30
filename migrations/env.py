"""Alembic environment.

The URL and SQLite connection arguments come from ``fpgen.db``, which reads
``fpgen.settings``. ``alembic.ini`` does not choose the database.
"""
from __future__ import annotations

from alembic import context

from fpgen.db import create_migration_engine
from fpgen.models import Base
from fpgen.settings import get_settings

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = get_settings().database_url
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_migration_engine()
    try:
        with connectable.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
