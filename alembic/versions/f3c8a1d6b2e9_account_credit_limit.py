"""accounts.credit_limit / credit_limit_as_of: a card's limit, for utilisation

Revision ID: f3c8a1d6b2e9
Revises: e5b9d2a7c4f1
Create Date: 2026-11-22

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f3c8a1d6b2e9'
down_revision: Union[str, Sequence[str], None] = 'e5b9d2a7c4f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # init_db() may already have added the columns on SQLite.
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("accounts")}
    with op.batch_alter_table("accounts") as batch_op:
        if "credit_limit" not in cols:
            batch_op.add_column(sa.Column("credit_limit", sa.Numeric(18, 2), nullable=True))
        if "credit_limit_as_of" not in cols:
            batch_op.add_column(sa.Column("credit_limit_as_of", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.drop_column("credit_limit_as_of")
        batch_op.drop_column("credit_limit")
