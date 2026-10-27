"""Concurrency proof: hammer one account with parallel debits.

This is the test that actually exercises atomicity + locking. It fires many
debits at the same instant against a *live* server and asserts the ledger never
breaks: the balance never goes negative, and it always equals
(credits - debits) of the transactions that actually persisted.

Usage:
    # terminal 1
    LEDGER_DATABASE_URL="sqlite:///./concurrency.db" uv run ledger-api
    # terminal 2
    uv run python scripts/concurrency_check.py
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor

import httpx

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
START_BALANCE = 100
DEBIT_AMOUNT = 10
N_PARALLEL = 20  # 20 x 10 = 200 requested against a balance of 100


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=30) as c:
        acc = c.post("/accounts", json={"name": "race"}).json()["id"]
        c.post(f"/accounts/{acc}/transactions",
               json={"type": "credit", "amount": START_BALANCE, "reference_id": "seed"})

        def debit(i: int) -> int:
            r = c.post(f"/accounts/{acc}/transactions",
                       json={"type": "debit", "amount": DEBIT_AMOUNT,
                             "reference_id": f"d{i}"})
            return r.status_code

        with ThreadPoolExecutor(max_workers=N_PARALLEL) as pool:
            codes = list(pool.map(debit, range(N_PARALLEL)))

        ok = codes.count(201)
        rejected = codes.count(422)
        balance = c.get(f"/accounts/{acc}/balance").json()["balance"]

        # Sum the persisted transactions directly.
        hist = c.get(f"/accounts/{acc}/transactions",
                     params={"limit": 100}).json()["transactions"]
        credited = sum(t["amount"] for t in hist if t["type"] == "credit")
        debited = sum(t["amount"] for t in hist if t["type"] == "debit")

        print(f"parallel debits: {N_PARALLEL}  succeeded: {ok}  rejected(422): {rejected}")
        print(f"final balance: {balance}  (credited {credited} - debited {debited})")

        expected_ok = START_BALANCE // DEBIT_AMOUNT
        checks = {
            "balance never negative": balance >= 0,
            "balance == credits - debits": balance == credited - debited,
            f"exactly {expected_ok} debits succeeded": ok == expected_ok,
            "no money overspent": debited <= START_BALANCE,
        }
        for label, passed in checks.items():
            print(f"  [{'PASS' if passed else 'FAIL'}] {label}")

        return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
