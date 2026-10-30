"""ORM models.

The jobs table is added with its migration in a later phase. Nothing here
creates tables, and API startup must not call ``create_all``.
"""
from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
