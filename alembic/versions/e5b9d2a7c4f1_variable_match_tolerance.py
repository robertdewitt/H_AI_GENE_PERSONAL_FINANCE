"""user_profile.variable_match_tolerance_pct: how close a variable payment must be

A variable scheduled payment (salary, a card bill) carries a placeholder
amount; a payment within this percentage of it settles the schedule.

Revision ID: e5b9d2a7c4f1
Revises: d4a8c3e1f2b7
Create Date: 2026-10-26

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e5b9d2a7c4f1'
down_revision: Union[str, Sequence[str], None] = 'd4a8c3e1f2b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # init_db() may already have added the column on SQLite.
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("user_profile")}
    if "variable_match_tolerance_pct" not in cols:
        with op.batch_alter_table("user_profile") as batch_op:
            batch_op.add_column(sa.Column("variable_match_tolerance_pct", sa.Integer(),
                                          nullable=True, server_default="30"))


def downgrade() -> None:
    with op.batch_alter_table("user_profile") as batch_op:
        batch_op.drop_column("variable_match_tolerance_pct")
