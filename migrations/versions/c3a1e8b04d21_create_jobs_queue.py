"""create jobs queue

Revision ID: c3a1e8b04d21
Revises:
Create Date: 2026-10-30

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = "c3a1e8b04d21"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamp() -> sa.DateTime:
    """UTC timestamps. Fractional seconds are kept on MySQL and MariaDB."""
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql").with_variant(
        mysql.DATETIME(fsp=6), "mariadb"
    )


def upgrade() -> None:
    """Create the job queue and its admission row."""
    op.create_table(
        "queue_coordination",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("admission_nonce", sa.BigInteger(), nullable=False),
        sa.Column("next_queue_position", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_queue_coordination_singleton"),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    coordination = sa.table(
        "queue_coordination",
        sa.column("id", sa.Integer),
        sa.column("admission_nonce", sa.BigInteger),
        sa.column("next_queue_position", sa.BigInteger),
    )
    op.bulk_insert(
        coordination,
        [{"id": 1, "admission_nonce": 0, "next_queue_position": 1}],
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("job_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("request_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", _timestamp(), nullable=False),
        sa.Column("started_at", _timestamp(), nullable=True),
        sa.Column("completed_at", _timestamp(), nullable=True),
        sa.Column("expires_at", _timestamp(), nullable=True),
        sa.Column("result_metadata", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("queue_position", sa.BigInteger(), nullable=False),
        sa.Column("recovery_count", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_jobs_status",
        ),
        sa.CheckConstraint(
            "job_type IN ('generate', 'render')",
            name="ck_jobs_job_type",
        ),
        sa.CheckConstraint("recovery_count >= 0", name="ck_jobs_recovery_count"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("queue_position", name="uq_jobs_queue_position"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_jobs_status_queue_position",
        "jobs",
        ["status", "queue_position"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the job queue."""
    op.drop_index("ix_jobs_status_queue_position", table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("queue_coordination")
