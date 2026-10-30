"""ORM models.

Tables are created by Alembic migrations, not by importing this module and
not by API startup. Do not call ``create_all`` from the API or worker.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.mysql import DATETIME as MySQLDateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC timestamps stored in a portable datetime column.

    MySQL and MariaDB keep microseconds (``DATETIME(6)``). Naive values are
    rejected so a local clock cannot be written by accident. Stored values are
    read back as UTC.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def load_dialect_impl(self, dialect):  # type: ignore[no-untyped-def]
        if dialect.name in {"mysql", "mariadb"}:
            return dialect.type_descriptor(MySQLDateTime(timezone=True, fsp=6))
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:  # type: ignore[no-untyped-def]
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:  # type: ignore[no-untyped-def]
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Job(Base):
    """One queued, running, or finished floor-plan job."""

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_jobs_status",
        ),
        CheckConstraint(
            "job_type IN ('generate', 'render')",
            name="ck_jobs_job_type",
        ),
        CheckConstraint("recovery_count >= 0", name="ck_jobs_recovery_count"),
        UniqueConstraint("queue_position", name="uq_jobs_queue_position"),
        Index("ix_jobs_status_queue_position", "status", "queue_position"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    request_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    result_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    queue_position: Mapped[int] = mapped_column(BigInteger, nullable=False)
    recovery_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class QueueCoordination(Base):
    """Single row updated inside each enqueue transaction.

    The update takes a lock on SQLite, MySQL, and MariaDB so two submissions
    cannot both pass the queued-job limit. ``next_queue_position`` is the
    FIFO ordering counter.
    """

    __tablename__ = "queue_coordination"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_queue_coordination_singleton"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    admission_nonce: Mapped[int] = mapped_column(BigInteger, nullable=False)
    next_queue_position: Mapped[int] = mapped_column(BigInteger, nullable=False)
