"""Сверка: что реально лежит на приёмнике

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-29
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("mailboxes") as batch:
        batch.add_column(sa.Column("dst_total_messages", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("dst_total_bytes", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("reconciled_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("mailboxes") as batch:
        batch.drop_column("reconciled_at")
        batch.drop_column("dst_total_bytes")
        batch.drop_column("dst_total_messages")
