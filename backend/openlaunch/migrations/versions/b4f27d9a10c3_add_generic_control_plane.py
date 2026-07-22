"""add generic tool and data control plane

Revision ID: b4f27d9a10c3
Revises: 42e2978c7933
"""

from alembic import op
import sqlalchemy as sa

revision = "b4f27d9a10c3"
down_revision = "42e2978c7933"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "data_connection",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("scope_type", sa.String(), nullable=False),
        sa.Column("scope_id", sa.String(), nullable=False),
        sa.Column("provider_type", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("safe_metadata", sa.Text(), nullable=False),
        sa.Column("secret_ref", sa.Text(), nullable=True),
        sa.Column("policy", sa.Text(), nullable=False),
        sa.Column("access_grants", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "tool_profile",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("assignments", sa.Text(), nullable=False),
        sa.Column("bundle", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "query_audit",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("actor_id", sa.String(), nullable=False),
        sa.Column("connection_id", sa.String(), nullable=False),
        sa.Column("provider_type", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("tool_call_id", sa.String(), nullable=False),
        sa.Column("objects", sa.Text(), nullable=False),
        sa.Column("policy_decision", sa.String(), nullable=False),
        sa.Column("query_fingerprint", sa.String(), nullable=False),
        sa.Column("raw_sql", sa.Text(), nullable=True),
        sa.Column("started_at", sa.BigInteger(), nullable=False),
        sa.Column("ended_at", sa.BigInteger(), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("result_bytes", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
    )
    op.create_index(
        "ix_query_audit_connection_started",
        "query_audit",
        ["connection_id", "started_at"],
    )
    op.create_table(
        "tool_profile_audit",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("profile_id", sa.String(), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=False),
        sa.Column("api_credential_id", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("endpoint", sa.String(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_tool_profile_audit_created", "tool_profile_audit", ["created_at"])


def downgrade():
    op.drop_index("ix_tool_profile_audit_created", table_name="tool_profile_audit")
    op.drop_table("tool_profile_audit")
    op.drop_index("ix_query_audit_connection_started", table_name="query_audit")
    op.drop_table("query_audit")
    op.drop_table("tool_profile")
    op.drop_table("data_connection")
