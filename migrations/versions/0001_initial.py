"""Начальная схема

Revision ID: 0001
Revises:
Create Date: 2026-07-29
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )

    op.create_table(
        "endpoints",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("preset", sa.String(length=64), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("security", sa.String(length=16), nullable=False),
        sa.Column("verify_cert", sa.Boolean(), nullable=False),
        sa.Column("auth_mode", sa.String(length=16), nullable=False),
        sa.Column("master_username", sa.String(length=255), nullable=True),
        sa.Column("master_secret_enc", sa.Text(), nullable=True),
        sa.Column("master_separator", sa.String(length=4), nullable=False),
        sa.Column("max_parallel", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("wizard_step", sa.String(length=32), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=True),
        sa.Column("src_endpoint_id", sa.Integer(), nullable=True),
        sa.Column("dst_endpoint_id", sa.Integer(), nullable=True),
        sa.Column("max_parallel", sa.Integer(), nullable=False),
        sa.Column("migrate_trash", sa.Boolean(), nullable=False),
        sa.Column("migrate_spam", sa.Boolean(), nullable=False),
        sa.Column("unknown_folder_policy", sa.String(length=16), nullable=False),
        sa.Column("unknown_folder_container", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(), nullable=False),
        sa.Column("credentials_purged_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["src_endpoint_id"], ["endpoints.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["dst_endpoint_id"], ["endpoints.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "mailboxes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("src_email", sa.String(length=320), nullable=False),
        sa.Column("dst_email", sa.String(length=320), nullable=False),
        sa.Column("src_password_enc", sa.Text(), nullable=True),
        sa.Column("dst_password_enc", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("src_check_result", sa.String(length=32), nullable=True),
        sa.Column("dst_check_result", sa.String(length=32), nullable=True),
        sa.Column("src_check_detail", sa.Text(), nullable=True),
        sa.Column("dst_check_detail", sa.Text(), nullable=True),
        sa.Column("auth_locked", sa.Boolean(), nullable=False),
        sa.Column("src_auth_attempts", sa.Integer(), nullable=False),
        sa.Column("dst_auth_attempts", sa.Integer(), nullable=False),
        sa.Column("total_messages", sa.Integer(), nullable=True),
        sa.Column("total_bytes", sa.Integer(), nullable=True),
        sa.Column("dst_quota_limit_bytes", sa.Integer(), nullable=True),
        sa.Column("dst_quota_used_bytes", sa.Integer(), nullable=True),
        sa.Column("done_messages", sa.Integer(), nullable=False),
        sa.Column("done_bytes", sa.Integer(), nullable=False),
        sa.Column("checked_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "src_email", name="uq_mailbox_project_src"),
    )
    op.create_index("ix_mailbox_project_status", "mailboxes", ["project_id", "status"])

    op.create_table(
        "mailbox_folders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("mailbox_id", sa.Integer(), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("name_raw", sa.String(length=1024), nullable=False),
        sa.Column("name_display", sa.String(length=1024), nullable=False),
        sa.Column("delimiter", sa.String(length=4), nullable=True),
        sa.Column("special_use", sa.String(length=32), nullable=True),
        sa.Column("selectable", sa.Boolean(), nullable=False),
        sa.Column("messages", sa.Integer(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("uidvalidity", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["mailbox_id"], ["mailboxes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_folder_mailbox_side", "mailbox_folders", ["mailbox_id", "side"])

    op.create_table(
        "folder_mappings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("src_name", sa.String(length=1024), nullable=False),
        sa.Column("dst_name", sa.String(length=1024), nullable=True),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("confirmed", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "src_name", name="uq_mapping_project_src"),
    )

    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("mailbox_id", sa.Integer(), nullable=True),
        sa.Column("level", sa.String(length=8), nullable=False),
        sa.Column("code", sa.String(length=48), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["mailbox_id"], ["mailboxes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_event_project_ts", "events", ["project_id", "ts"])
    op.create_index("ix_event_mailbox_ts", "events", ["mailbox_id", "ts"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=True),
        sa.Column("target_id", sa.Integer(), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_ts", "audit_log", ["ts"])


def downgrade() -> None:
    op.drop_index("ix_audit_ts", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_event_mailbox_ts", table_name="events")
    op.drop_index("ix_event_project_ts", table_name="events")
    op.drop_table("events")
    op.drop_table("folder_mappings")
    op.drop_index("ix_folder_mailbox_side", table_name="mailbox_folders")
    op.drop_table("mailbox_folders")
    op.drop_index("ix_mailbox_project_status", table_name="mailboxes")
    op.drop_table("mailboxes")
    op.drop_table("projects")
    op.drop_table("endpoints")
    op.drop_table("users")
