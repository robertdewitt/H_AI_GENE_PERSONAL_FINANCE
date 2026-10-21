"""scheduled_payments.counter_account_id: the other side of a transfer

A scheduled payment lived on one account only, so a mortgage payment made
from checking showed up in one of the two forecasts, never both. The counter
account lets the forecast mirror each occurrence into the receiving account.

Revision ID: c7e2f91a4b10
Revises: b1c4a7e35d90
Create Date: 2026-10-21

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c7e2f91a4b10'
down_revision: Union[str, Sequence[str], None] = 'b1c4a7e35d90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # init_db() may already have added the column on SQLite.
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("scheduled_payments")}
    with op.batch_alter_table("scheduled_payments") as batch_op:
        if "counter_account_id" not in cols:
            batch_op.add_column(sa.Column("counter_account_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_scheduled_payments_counter_account_id_accounts",
            "accounts", ["counter_account_id"], ["id"],
        )
        batch_op.create_index(
            "ix_scheduled_payments_counter_account_id", ["counter_account_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("scheduled_payments") as batch_op:
        batch_op.drop_index("ix_scheduled_payments_counter_account_id")
        batch_op.drop_constraint(
            "fk_scheduled_payments_counter_account_id_accounts", type_="foreignkey",
        )
        batch_op.drop_column("counter_account_id")
