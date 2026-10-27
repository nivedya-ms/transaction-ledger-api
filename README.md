# Transaction Ledger API

A backend API for a simple transaction ledger: create accounts, post credit/debit
transactions, and read balances and history — with correctness under concurrency
as the primary design goal.

**Stack:** Python 3.12 · FastAPI · SQLAlchemy 2 · SQLite · managed with [uv].

---

## Running it

Prerequisite: [uv] installed (`brew install uv`). uv fetches the right Python and
all dependencies for you.

```bash
# from the ledger-api/ folder
uv sync                                          # install deps (+ Python 3.12)

# start the server
uv run uvicorn ledger_api.main:app --app-dir src --port 8000
```

The API is now at `http://127.0.0.1:8000`.
Interactive API docs (Swagger UI) are served at **http://127.0.0.1:8000/docs**.

### Tests

```bash
uv run pytest                                    # functional test suite (17 tests)
```

### Concurrency check (the important one)

Proves the ledger stays correct when many debits hit one account at once.

```bash
# terminal 1 — a throwaway DB so it doesn't touch your main one
LEDGER_DATABASE_URL="sqlite:///./concurrency.db" uv run uvicorn ledger_api.main:app --app-dir src --port 8000

# terminal 2
uv run python scripts/concurrency_check.py
```

---

## API

All error responses share one shape:

```json
{ "error": { "code": "insufficient_balance", "message": "…", "details": { } } }
```

| Method & path | Purpose | Success |
|---|---|---|
| `POST /accounts` | Create an account | `201` |
| `GET /accounts/{id}` | Get an account | `200` |
| `GET /accounts/{id}/balance` | Get an account's balance | `200` |
| `POST /accounts/{id}/transactions` | Add a credit/debit | `201` |
| `GET /accounts/{id}/transactions` | List history (filter/paginate) | `200` |
| `POST /transfers` | Transfer between accounts (bonus) | `201` |

### Create account

```bash
curl -X POST localhost:8000/accounts -H 'content-type: application/json' \
  -d '{"name": "Ada"}'
```
```json
{ "id": 1, "name": "Ada", "balance": 0, "created_at": "2026-10-27T…Z" }
```
Balance always starts at `0`; the client cannot set `id` or `balance` (extra fields
are rejected with `400`).

### Add a transaction

```bash
curl -X POST localhost:8000/accounts/1/transactions -H 'content-type: application/json' \
  -d '{"type": "debit", "amount": 30, "reference_id": "abc123", "description": "coffee"}'
```
- `type` ∈ `credit` | `debit`; `amount` is a positive whole number; `reference_id`
  is required.
- A **debit beyond the balance** is rejected with `422` and the balance is unchanged.
- Re-sending the **same `reference_id` for the same account** does **not** create a
  second transaction or move the balance — it returns `409` with the original
  transaction under `error.details.transaction`. (The same `reference_id` is allowed
  on a *different* account.)

### Transaction history

```
GET /accounts/1/transactions?type=debit&page=1&limit=20
```
- `type` filters (omit for both). `page` ≥ 1, `limit` 1–100 (default 20).
- Sorted **latest-first** (`created_at` desc, with `id` as a tiebreaker so pages
  never overlap or skip).
```json
{
  "pagination": { "current_page": 1, "page_size": 20, "total_records": 3, "total_pages": 1 },
  "transactions": [ { "id": 7, "account_id": 1, "type": "debit", "amount": 30, … } ]
}
```

### Transfer (bonus)

```bash
curl -X POST localhost:8000/transfers -H 'content-type: application/json' \
  -d '{"from_account_id": 1, "to_account_id": 2, "amount": 30, "reference_id": "xfer-1"}'
```
Debit + credit happen in one atomic transaction — it fully succeeds or fully rolls
back; a partial transfer is impossible.

### Status codes

| Code | When |
|---|---|
| `201` | Resource created |
| `200` | Successful read |
| `400` | Invalid request data (bad/missing fields, bad pagination, same-account transfer) |
| `404` | Account does not exist |
| `409` | Duplicate `reference_id` for the account (idempotency) |
| `422` | Debit/transfer with insufficient balance (valid request, can't be fulfilled) |

---

## How the integrity rules are enforced

- **Atomicity** — saving a transaction and updating the balance are one DB
  transaction; a crash or error rolls back both (`crud.py`).
- **Concurrency** — every transaction runs `BEGIN IMMEDIATE`, so writers to the same
  account serialize: the second request waits, then reads the fresh balance instead
  of a stale one. `PRAGMA busy_timeout` makes waiters wait rather than error
  (`database.py`). Verified by `scripts/concurrency_check.py`.
- **Balance consistency** — balance is a stored integer, always updated inside the
  same transaction as the row that explains it; a `CHECK(balance >= 0)` constraint is
  the final backstop.
- **Idempotency** — `UNIQUE(account_id, reference_id)` plus an up-front lookup; the
  unique constraint also closes the concurrent-duplicate race.
- **Immutability** — no update/delete endpoints exist, **and** database triggers
  reject any `UPDATE`/`DELETE` on a transaction row (`models.py`).
- **Money** — stored as whole integers; never floats (no rounding drift).

## Assumptions

- **Amounts are whole-number integers** (no decimals/cents); `amount` must be `> 0`.
- **No authentication/authorization** — the API is open, as the brief scopes it.
  Account `name` is not unique. Transaction `id`s are sequential integers; since
  there is no access control, enumeration is not a concern here (in a real app with
  auth, I'd expose a UUID instead).
- **A rejected transaction is not persisted**, so a `reference_id` used by a rejected
  debit can be reused later.
- **Timestamps** are stored in UTC.
- **Duplicate `reference_id`** returns `409` (with the original transaction) rather
  than silently succeeding — the caller can tell it was a replay and reconcile.
- **Transfer** applies the same `reference_id` to both legs; both accounts must
  exist and differ; retrying the same `reference_id` returns the original (`409`).
- `SQLite` is used for zero-setup local running; the concurrency approach
  (serialized writers) is correct but trades some throughput — appropriate for this
  assessment.

[uv]: https://docs.astral.sh/uv/
