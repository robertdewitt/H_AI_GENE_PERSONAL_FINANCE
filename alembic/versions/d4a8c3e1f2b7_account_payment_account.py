"""accounts.payment_account_id: the account that pays a card's statement

Lets a credit card's monthly statement payment be scheduled as a transfer
from the account that actually pays it, so it shows in both forecasts.

Revision ID: d4a8c3e1f2b7
Revises: c7e2f91a4b10
Create Date: 2026-10-25

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd4a8c3e1f2b7'
down_revision: Union[str, Sequence[str], None] = 'c7e2f91a4b10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # init_db() may already have added the column on SQLite.
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("accounts")}
    with op.batch_alter_table("accounts") as batch_op:
        if "payment_account_id" not in cols:
            batch_op.add_column(sa.Column("payment_account_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_accounts_payment_account_id_accounts",
            "accounts", ["payment_account_id"], ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.drop_constraint("fk_accounts_payment_account_id_accounts", type_="foreignkey")
        batch_op.drop_column("payment_account_id")
