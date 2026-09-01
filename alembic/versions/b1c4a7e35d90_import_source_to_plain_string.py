"""import_batches.source: DB enum -> plain string

The column was Enum(ImportSource), which resolves the stored text back to a
member on every read. The Revolut PDF importer wrote "revolut_pdf", which was
never a member, so any load of those batches raised

    LookupError: 'revolut_pdf' is not among the defined enum values

A plain VARCHAR removes that failure mode; the enum stays in the model as the
vocabulary callers write through. Existing rows hold member NAMES
("MANUAL_UPLOAD") alongside the two stray values, so they are folded to the
lower-case values for one consistent representation.

Revision ID: b1c4a7e35d90
Revises: 9ff9063ce318
Create Date: 2026-08-28

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b1c4a7e35d90'
down_revision: Union[str, Sequence[str], None] = '9ff9063ce318'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Widen the column, then normalise the values it holds."""
    with op.batch_alter_table("import_batches") as batch_op:
        batch_op.alter_column(
            "source",
            existing_type=sa.VARCHAR(length=13),
            type_=sa.String(length=30),
            existing_nullable=False,
            postgresql_using="source::text",
        )
    op.execute(
        "UPDATE import_batches SET source = LOWER(source) "
        "WHERE source <> LOWER(source)"
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS importsource")


def downgrade() -> None:
    """Back to member names in a 13-char column.

    Rows whose source has no matching member ("revolut_pdf" before it was
    added, anything a later writer invents) cannot be represented by the old
    enum, so they go back to the default rather than blocking the downgrade.
    """
    op.execute(
        "UPDATE import_batches SET source = 'manual_upload' "
        "WHERE source NOT IN ('manual_upload', 'automated')"
    )
    op.execute("UPDATE import_batches SET source = UPPER(source)")
    with op.batch_alter_table("import_batches") as batch_op:
        batch_op.alter_column(
            "source",
            existing_type=sa.String(length=30),
            type_=sa.VARCHAR(length=13),
            existing_nullable=False,
        )
