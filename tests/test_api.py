"""Functional tests for the Transaction Ledger API.

Each test run uses a throwaway SQLite file (set before the app is imported).
Concurrency is verified separately by scripts/concurrency_check.py against a
live server, since that needs real parallel connections.
"""

import os
import tempfile

# Point the app at a throwaway DB *before* importing it.
_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_db_fd)
os.environ["LEDGER_DATABASE_URL"] = f"sqlite:///{_db_path}"

import pytest
from fastapi.testclient import TestClient

from ledger_api.database import engine
from ledger_api.main import app
from ledger_api.models import Base


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _account(client, name="Ada") -> int:
    r = client.post("/accounts", json={"name": name})
    assert r.status_code == 201
    return r.json()["id"]


def _tx(client, account_id, **body):
    return client.post(f"/accounts/{account_id}/transactions", json=body)


# --- Account creation -------------------------------------------------------
def test_create_account_starts_at_zero(client):
    r = client.post("/accounts", json={"name": "Ada"})
    assert r.status_code == 201
    data = r.json()
    assert data["balance"] == 0
    assert data["name"] == "Ada"
    assert "id" in data and "created_at" in data


def test_name_is_required(client):
    assert client.post("/accounts", json={"name": ""}).status_code == 400
    assert client.post("/accounts", json={}).status_code == 400


def test_client_cannot_set_initial_balance(client):
    # extra="forbid" rejects the sneaky field.
    r = client.post("/accounts", json={"name": "Ada", "balance": 999})
    assert r.status_code == 400


# --- Credit / debit math ----------------------------------------------------
def test_brief_example_sequence(client):
    # 0 +100 +50 -30 -20 = 100
    acc = _account(client)
    for i, (t, amt) in enumerate(
        [("credit", 100), ("credit", 50), ("debit", 30), ("debit", 20)]
    ):
        assert _tx(client, acc, type=t, amount=amt, reference_id=f"r{i}").status_code == 201
    assert client.get(f"/accounts/{acc}/balance").json()["balance"] == 100


def test_debit_insufficient_is_rejected_422_and_balance_unchanged(client):
    acc = _account(client)
    _tx(client, acc, type="credit", amount=100, reference_id="seed")
    r = _tx(client, acc, type="debit", amount=150, reference_id="too-big")
    assert r.status_code == 422
    assert client.get(f"/accounts/{acc}/balance").json()["balance"] == 100


def test_amount_must_be_positive(client):
    acc = _account(client)
    assert _tx(client, acc, type="credit", amount=0, reference_id="z").status_code == 400
    assert _tx(client, acc, type="credit", amount=-5, reference_id="n").status_code == 400


def test_bad_type_is_rejected(client):
    acc = _account(client)
    assert _tx(client, acc, type="withdraw", amount=5, reference_id="w").status_code == 400


def test_transaction_on_missing_account_is_404(client):
    assert _tx(client, 9999, type="credit", amount=5, reference_id="x").status_code == 404


# --- Idempotency ------------------------------------------------------------
def test_duplicate_reference_same_account_is_conflict_no_change(client):
    acc = _account(client)
    assert _tx(client, acc, type="credit", amount=100, reference_id="dup").status_code == 201
    r = _tx(client, acc, type="credit", amount=100, reference_id="dup")
    assert r.status_code == 409
    # Original transaction handed back, balance unchanged.
    assert r.json()["error"]["details"]["transaction"]["reference_id"] == "dup"
    assert client.get(f"/accounts/{acc}/balance").json()["balance"] == 100


def test_same_reference_allowed_across_accounts(client):
    a = _account(client, "A")
    b = _account(client, "B")
    assert _tx(client, a, type="credit", amount=10, reference_id="shared").status_code == 201
    assert _tx(client, b, type="credit", amount=10, reference_id="shared").status_code == 201


# --- History: filter, pagination, sort --------------------------------------
def test_history_filter_paginate_and_sort(client):
    acc = _account(client)
    for i in range(5):
        _tx(client, acc, type="credit", amount=10, reference_id=f"c{i}")
    for i in range(3):
        _tx(client, acc, type="debit", amount=5, reference_id=f"d{i}")

    # Filter: debits only.
    r = client.get(f"/accounts/{acc}/transactions", params={"type": "debit"})
    body = r.json()
    assert body["pagination"]["total_records"] == 3
    assert all(t["type"] == "debit" for t in body["transactions"])

    # No filter: both types.
    r = client.get(f"/accounts/{acc}/transactions")
    assert r.json()["pagination"]["total_records"] == 8

    # Pagination metadata.
    r = client.get(f"/accounts/{acc}/transactions", params={"page": 1, "limit": 3})
    meta = r.json()["pagination"]
    assert meta == {
        "current_page": 1, "page_size": 3, "total_records": 8, "total_pages": 3
    }
    assert len(r.json()["transactions"]) == 3

    # Sort: latest first (last created has the highest id).
    ids = [t["id"] for t in r.json()["transactions"]]
    assert ids == sorted(ids, reverse=True)


def test_invalid_pagination_is_400(client):
    acc = _account(client)
    assert client.get(f"/accounts/{acc}/transactions", params={"page": 0}).status_code == 400
    assert client.get(f"/accounts/{acc}/transactions", params={"limit": 0}).status_code == 400


def test_history_missing_account_is_404(client):
    assert client.get("/accounts/9999/transactions").status_code == 404


# --- Immutability -----------------------------------------------------------
def test_no_update_or_delete_routes(client):
    acc = _account(client)
    r = _tx(client, acc, type="credit", amount=10, reference_id="imm")
    tx_id = r.json()["id"]
    # The transactions path is registered for GET/POST only, so mutating verbs
    # on it are rejected (405). And no per-transaction edit/delete path exists
    # at all (404). Either way, there's no way to change a transaction.
    assert client.put(f"/accounts/{acc}/transactions").status_code == 405
    assert client.delete(f"/accounts/{acc}/transactions").status_code == 405
    assert client.put(f"/accounts/{acc}/transactions/{tx_id}", json={}).status_code in (404, 405)
    assert client.delete(f"/accounts/{acc}/transactions/{tx_id}").status_code in (404, 405)


def test_db_trigger_blocks_transaction_update(client):
    # Direct DB-level proof that the triggers reject any edit or delete.
    from sqlalchemy import text

    acc = _account(client)
    tx_id = _tx(client, acc, type="credit", amount=10, reference_id="trig").json()["id"]

    with pytest.raises(Exception):
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE transactions SET amount = 1 WHERE id = :i"), {"i": tx_id}
            )
    with pytest.raises(Exception):
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM transactions WHERE id = :i"), {"i": tx_id})


# --- Transfer (bonus) -------------------------------------------------------
def test_transfer_moves_money_atomically(client):
    a = _account(client, "A")
    b = _account(client, "B")
    _tx(client, a, type="credit", amount=100, reference_id="seedA")
    _tx(client, b, type="credit", amount=50, reference_id="seedB")

    r = client.post("/transfers", json={
        "from_account_id": a, "to_account_id": b,
        "amount": 30, "reference_id": "t1",
    })
    assert r.status_code == 201
    assert client.get(f"/accounts/{a}/balance").json()["balance"] == 70
    assert client.get(f"/accounts/{b}/balance").json()["balance"] == 80


def test_transfer_insufficient_rolls_back(client):
    a = _account(client, "A")
    b = _account(client, "B")
    _tx(client, a, type="credit", amount=10, reference_id="seedA")

    r = client.post("/transfers", json={
        "from_account_id": a, "to_account_id": b,
        "amount": 999, "reference_id": "t2",
    })
    assert r.status_code == 422
    # Nothing moved.
    assert client.get(f"/accounts/{a}/balance").json()["balance"] == 10
    assert client.get(f"/accounts/{b}/balance").json()["balance"] == 0
