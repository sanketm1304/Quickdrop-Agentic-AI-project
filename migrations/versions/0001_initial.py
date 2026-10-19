"""Initial operational schema with immutable audit events."""

from alembic import op

import app.models  # noqa: F401
from app.database import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind)
    if bind.dialect.name == "postgresql":
        op.execute("""
            CREATE FUNCTION prevent_audit_mutation() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'audit_events are append only'; END;
            $$ LANGUAGE plpgsql;
        """)
        op.execute("""
            CREATE TRIGGER audit_events_immutable BEFORE UPDATE OR DELETE ON audit_events
            FOR EACH ROW EXECUTE FUNCTION prevent_audit_mutation();
        """)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS audit_events_immutable ON audit_events")
        op.execute("DROP FUNCTION IF EXISTS prevent_audit_mutation()")
    Base.metadata.drop_all(bind)
