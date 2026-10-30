"""Worker entry point.

``python -m fpgen.worker`` loads the same configuration as the API and
migrations and checks that the database can be opened. Job processing is a
later phase. An API key is not required.
"""
from __future__ import annotations

from sqlalchemy import text

from fpgen.db import get_engine
from fpgen.settings import Settings, get_settings


def load_runtime() -> Settings:
    settings = get_settings()
    engine = get_engine(settings)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    finally:
        engine.dispose()
    return settings


def main() -> int:
    load_runtime()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
