"""Состояние переноса: попытки, текущая папка, код возврата, файл лога

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-29
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch_alter_table обязателен: SQLite не умеет ALTER TABLE в общем виде,
    # а обновляться будут посреди идущей миграции с живыми данными.
    with op.batch_alter_table("mailboxes") as batch:
        batch.add_column(
            sa.Column("run_attempts", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("current_folder", sa.String(length=1024), nullable=True))
        batch.add_column(sa.Column("last_exit_code", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("last_error", sa.Text(), nullable=True))
        batch.add_column(sa.Column("log_filename", sa.String(length=255), nullable=True))

    with op.batch_alter_table("projects") as batch:
        batch.add_column(
            sa.Column("max_message_size_mb", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("max_message_size_mb")

    with op.batch_alter_table("mailboxes") as batch:
        batch.drop_column("log_filename")
        batch.drop_column("last_error")
        batch.drop_column("last_exit_code")
        batch.drop_column("current_folder")
        batch.drop_column("run_attempts")
