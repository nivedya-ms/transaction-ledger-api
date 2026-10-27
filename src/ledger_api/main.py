"""FastAPI application: routes, consistent error handling, and startup.

Endpoints
---------
POST   /accounts                              create an account
GET    /accounts/{id}                         fetch an account
GET    /accounts/{id}/balance                 fetch an account's balance
POST   /accounts/{id}/transactions            add a credit/debit transaction
GET    /accounts/{id}/transactions            list transactions (filter/paginate)
POST   /transfers                             transfer between accounts (bonus)

Interactive API docs are served at /docs (Swagger UI) and /redoc.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Query
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import crud
from .database import engine, get_session
from .models import Base
from .schemas import (
    AccountCreate,
    AccountOut,
    BalanceOut,
    PageMeta,
    TransactionCreate,
    TransactionHistoryOut,
    TransactionOut,
    TransactionType,
    TransferCreate,
    TransferOut,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables (and the immutability triggers) on startup.
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="Transaction Ledger API", version="1.0.0", lifespan=lifespan)


# --- Consistent error responses ---------------------------------------------
def _error(status_code: int, code: str, message: str, details=None) -> JSONResponse:
    body: dict = {"error": {"code": code, "message": message}}
    if details is not None:
        body["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=body)


@app.exception_handler(RequestValidationError)
async def _validation_error(request, exc: RequestValidationError) -> JSONResponse:
    # Design choice: invalid request data -> 400 (422 is reserved for the
    # "valid request, can't be fulfilled" case, i.e. insufficient balance).
    return _error(400, "validation_error", "Invalid request data.",
                  jsonable_encoder(exc.errors()))


@app.exception_handler(crud.AccountNotFound)
async def _account_not_found(request, exc: crud.AccountNotFound) -> JSONResponse:
    return _error(404, "account_not_found",
                  f"Account {exc.account_id} does not exist.")


@app.exception_handler(crud.InsufficientBalance)
async def _insufficient_balance(request, exc: crud.InsufficientBalance) -> JSONResponse:
    return _error(422, "insufficient_balance",
                  "Account has insufficient balance for this debit.")


@app.exception_handler(crud.DuplicateReference)
async def _duplicate_reference(request, exc: crud.DuplicateReference) -> JSONResponse:
    # Idempotency: hand back the original transaction so the caller can reconcile.
    return _error(
        409, "duplicate_reference",
        "A transaction with this reference_id already exists for this account.",
        {"transaction": jsonable_encoder(TransactionOut.model_validate(exc.existing))},
    )


@app.exception_handler(crud.InvalidTransfer)
async def _invalid_transfer(request, exc: crud.InvalidTransfer) -> JSONResponse:
    return _error(400, "invalid_transfer", exc.message)


@app.exception_handler(StarletteHTTPException)
async def _http_exception(request, exc: StarletteHTTPException) -> JSONResponse:
    message = exc.detail if isinstance(exc.detail, str) else "Error."
    return _error(exc.status_code, "http_error", message)


# --- Routes -----------------------------------------------------------------
@app.get("/health", tags=["health"])
def health() -> dict:
    return {"status": "ok"}


@app.post("/accounts", response_model=AccountOut, status_code=201, tags=["accounts"])
def create_account(payload: AccountCreate, db: Session = Depends(get_session)):
    return crud.create_account(db, payload.name)


@app.get("/accounts/{account_id}", response_model=AccountOut, tags=["accounts"])
def get_account(account_id: int, db: Session = Depends(get_session)):
    return crud.get_account(db, account_id)


@app.get("/accounts/{account_id}/balance", response_model=BalanceOut, tags=["accounts"])
def get_balance(account_id: int, db: Session = Depends(get_session)):
    account = crud.get_account(db, account_id)
    return BalanceOut(account_id=account.id, balance=account.balance)


@app.post(
    "/accounts/{account_id}/transactions",
    response_model=TransactionOut,
    status_code=201,
    tags=["transactions"],
)
def add_transaction(
    account_id: int,
    payload: TransactionCreate,
    db: Session = Depends(get_session),
):
    return crud.add_transaction(db, account_id, payload)


@app.get(
    "/accounts/{account_id}/transactions",
    response_model=TransactionHistoryOut,
    tags=["transactions"],
)
def list_transactions(
    account_id: int,
    type: TransactionType | None = Query(default=None, description="Filter by type"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_session),
):
    rows, total, total_pages = crud.list_transactions(db, account_id, type, page, limit)
    return TransactionHistoryOut(
        pagination=PageMeta(
            current_page=page,
            page_size=limit,
            total_records=total,
            total_pages=total_pages,
        ),
        transactions=rows,
    )


@app.post("/transfers", response_model=TransferOut, status_code=201, tags=["transfers"])
def transfer(payload: TransferCreate, db: Session = Depends(get_session)):
    result = crud.transfer(db, payload)
    return TransferOut(
        debit=TransactionOut.model_validate(result["debit"]),
        credit=TransactionOut.model_validate(result["credit"]),
        from_account=BalanceOut(
            account_id=result["source"].id, balance=result["source"].balance
        ),
        to_account=BalanceOut(
            account_id=result["dest"].id, balance=result["dest"].balance
        ),
    )
