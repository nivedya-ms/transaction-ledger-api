"""Business logic for accounts, transactions, and transfers.

Every write runs inside a single SQLAlchemy transaction. Because the engine
starts each transaction with ``BEGIN IMMEDIATE`` (see database.py), writers are
serialized: a second request for the same account waits for the first to commit,
then reads the fresh balance. The ``CHECK(balance >= 0)`` and
``UNIQUE(account_id, reference_id)`` constraints are the final backstops.
"""

from __future__ import annotations

from math import ceil

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import Account, Transaction
from .schemas import TransactionCreate, TransactionType, TransferCreate


# --- Domain errors (mapped to HTTP status codes in main.py) -----------------
class AccountNotFound(Exception):
    def __init__(self, account_id: int) -> None:
        self.account_id = account_id


class InsufficientBalance(Exception):
    pass


class DuplicateReference(Exception):
    """A transaction with this (account_id, reference_id) already exists."""

    def __init__(self, existing: Transaction) -> None:
        self.existing = existing


class InvalidTransfer(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


# --- Accounts ---------------------------------------------------------------
def create_account(db: Session, name: str) -> Account:
    account = Account(name=name, balance=0)
    db.add(account)
    db.commit()
    return account


def get_account(db: Session, account_id: int) -> Account:
    account = db.get(Account, account_id)
    if account is None:
        raise AccountNotFound(account_id)
    return account


# --- Transactions -----------------------------------------------------------
def _find_existing(db: Session, account_id: int, reference_id: str) -> Transaction | None:
    return db.scalar(
        select(Transaction).where(
            Transaction.account_id == account_id,
            Transaction.reference_id == reference_id,
        )
    )


def add_transaction(
    db: Session, account_id: int, payload: TransactionCreate
) -> Transaction:
    # The write lock is already held (BEGIN IMMEDIATE) from this first read on.
    account = db.get(Account, account_id)
    if account is None:
        raise AccountNotFound(account_id)

    # Idempotency: same reference on same account -> return original, no change.
    existing = _find_existing(db, account_id, payload.reference_id)
    if existing is not None:
        raise DuplicateReference(existing)

    if payload.type == TransactionType.debit:
        if account.balance < payload.amount:
            raise InsufficientBalance()
        account.balance -= payload.amount
    else:  # credit
        account.balance += payload.amount

    tx = Transaction(
        account_id=account_id,
        type=payload.type.value,
        amount=payload.amount,
        description=payload.description,
        reference_id=payload.reference_id,
    )
    db.add(tx)
    # Save the transaction AND the new balance atomically (all-or-nothing).
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Backstop for the concurrent-duplicate race: the UNIQUE constraint fired.
        existing = _find_existing(db, account_id, payload.reference_id)
        if existing is not None:
            raise DuplicateReference(existing)
        raise
    return tx


def list_transactions(
    db: Session,
    account_id: int,
    type_filter: TransactionType | None,
    page: int,
    limit: int,
) -> tuple[list[Transaction], int, int]:
    if db.get(Account, account_id) is None:
        raise AccountNotFound(account_id)

    conditions = [Transaction.account_id == account_id]
    if type_filter is not None:
        conditions.append(Transaction.type == type_filter.value)

    total = db.scalar(
        select(func.count()).select_from(Transaction).where(*conditions)
    )
    rows = list(
        db.scalars(
            select(Transaction)
            .where(*conditions)
            # Latest first; id as a tiebreaker so pagination is deterministic
            # even when two rows share a created_at timestamp.
            .order_by(Transaction.created_at.desc(), Transaction.id.desc())
            .offset((page - 1) * limit)
            .limit(limit)
        )
    )
    total_pages = ceil(total / limit) if total else 0
    return rows, total, total_pages


# --- Transfer (bonus) -------------------------------------------------------
def transfer(db: Session, payload: TransferCreate) -> dict:
    if payload.from_account_id == payload.to_account_id:
        raise InvalidTransfer("Cannot transfer to the same account.")

    source = db.get(Account, payload.from_account_id)
    if source is None:
        raise AccountNotFound(payload.from_account_id)
    dest = db.get(Account, payload.to_account_id)
    if dest is None:
        raise AccountNotFound(payload.to_account_id)

    # Idempotency: a retried transfer (same reference_id) returns the original.
    existing = _find_existing(db, source.id, payload.reference_id)
    if existing is not None:
        raise DuplicateReference(existing)

    if source.balance < payload.amount:
        raise InsufficientBalance()

    source.balance -= payload.amount
    dest.balance += payload.amount

    debit_tx = Transaction(
        account_id=source.id,
        type="debit",
        amount=payload.amount,
        description=payload.description,
        reference_id=payload.reference_id,
    )
    credit_tx = Transaction(
        account_id=dest.id,
        type="credit",
        amount=payload.amount,
        description=payload.description,
        reference_id=payload.reference_id,
    )
    db.add_all([debit_tx, credit_tx])
    # Both legs (and both balance updates) commit together or not at all.
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = _find_existing(db, source.id, payload.reference_id)
        if existing is not None:
            raise DuplicateReference(existing)
        raise
    return {
        "debit": debit_tx,
        "credit": credit_tx,
        "source": source,
        "dest": dest,
    }
