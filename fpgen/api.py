"""FastAPI application.

Business routes and bearer-token checks are added later. ``GET /health``
stays unauthenticated, so do not attach a global authentication dependency.
The API key is required only so the process refuses to start without one.
"""
from __future__ import annotations

import sys

from fastapi import FastAPI

from fpgen.db import get_engine
from fpgen.settings import ConfigurationError, get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    settings.require_api_key()
    engine = get_engine(settings)
    application = FastAPI(title="Floor Plan API")
    application.state.settings = settings
    application.state.engine = engine

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


def _application_or_exit() -> FastAPI:
    try:
        return create_app()
    except ConfigurationError as exc:
        message = str(exc)
    sys.stderr.write(f"ERROR: {message}\n")
    raise SystemExit(1)


app = _application_or_exit()
