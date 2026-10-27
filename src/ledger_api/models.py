"""SQLAlchemy ORM models plus database-level integrity guards.

The database is the final line of defence, not just the application code:
- balances can never go negative (CHECK),
- a reference_id is unique per account (UNIQUE) -> idempotency,
- transactions are immutable (triggers reject any UPDATE/DELETE).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    DDL,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # auto-incrementing
    name: Mapped[str] = mapped_column(String, nullable=False)
    # Money is stored as whole integers -> no float rounding drift.
    balance: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="account")

    __table_args__ = (
        CheckConstraint("balance >= 0", name="ck_accounts_balance_non_negative"),
    )


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # auto-incrementing
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id"), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(String, nullable=False)  # "credit" | "debit"
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    reference_id: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    account: Mapped["Account"] = relationship(back_populates="transactions")

    __table_args__ = (
        # Idempotency: a reference_id is unique per account (reusable across accounts).
        UniqueConstraint("account_id", "reference_id", name="uq_tx_account_reference"),
        CheckConstraint("amount > 0", name="ck_tx_amount_positive"),
        CheckConstraint("type IN ('credit', 'debit')", name="ck_tx_type_valid"),
    )


# --- Immutability at the database level -------------------------------------
# Belt-and-suspenders: besides exposing no update/delete routes, the database
# itself refuses to modify or delete a transaction row.
_block_update = DDL(
    "CREATE TRIGGER IF NOT EXISTS trg_tx_no_update "
    "BEFORE UPDATE ON transactions "
    "BEGIN SELECT RAISE(ABORT, 'transactions are immutable'); END"
)
_block_delete = DDL(
    "CREATE TRIGGER IF NOT EXISTS trg_tx_no_delete "
    "BEFORE DELETE ON transactions "
    "BEGIN SELECT RAISE(ABORT, 'transactions cannot be deleted'); END"
)
event.listen(Transaction.__table__, "after_create", _block_update)
event.listen(Transaction.__table__, "after_create", _block_delete)
