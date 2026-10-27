"""Request and response schemas (validation + serialization).

`extra="forbid"` on the request bodies means a client that tries to sneak in a
field it shouldn't set (e.g. `balance` or `id` on account creation) gets a
validation error instead of having it silently ignored.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class TransactionType(str, Enum):
    credit = "credit"
    debit = "debit"


# --- Requests ---------------------------------------------------------------
class AccountCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(min_length=1, description="Account holder name")


class TransactionCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    type: TransactionType
    amount: int = Field(gt=0, description="Whole-number amount, must be > 0")
    reference_id: str = Field(min_length=1)
    description: str | None = Field(default=None)


class TransferCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    from_account_id: int
    to_account_id: int
    amount: int = Field(gt=0)
    reference_id: str = Field(min_length=1)
    description: str | None = Field(default=None)


# --- Responses --------------------------------------------------------------
class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    balance: int
    created_at: datetime


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    type: str
    amount: int
    description: str | None
    reference_id: str
    created_at: datetime


class BalanceOut(BaseModel):
    account_id: int
    balance: int


class PageMeta(BaseModel):
    current_page: int
    page_size: int
    total_records: int
    total_pages: int


class TransactionHistoryOut(BaseModel):
    pagination: PageMeta
    transactions: list[TransactionOut]


class TransferOut(BaseModel):
    debit: TransactionOut
    credit: TransactionOut
    from_account: BalanceOut
    to_account: BalanceOut
