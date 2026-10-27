"""Database engine, session factory, and SQLite tuning.

SQLite needs a little care to behave safely under concurrency:

- Foreign keys are OFF by default, so we turn them on for every connection.
- We want writers to *serialize* cleanly (the design decision for this project):
  so every transaction starts with ``BEGIN IMMEDIATE``, which grabs the write
  lock up front. A second request for the same data then *waits* for the first
  to commit (up to ``busy_timeout``) instead of reading a stale balance.

This is the documented SQLAlchemy recipe for taking control of SQLite's
transaction handling: disable pysqlite's implicit ``BEGIN`` and emit our own.
"""

from __future__ import annotations

import os
from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

# Configurable so tests can point at a throwaway database file.
DATABASE_URL = os.environ.get("LEDGER_DATABASE_URL", "sqlite:///./ledger.db")

# check_same_thread=False: FastAPI serves sync endpoints from a threadpool, so a
# pooled connection may be used from a different thread than it was created on.
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _sqlite_on_connect(dbapi_connection, connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")        # enforce FK constraints
    cursor.execute("PRAGMA busy_timeout=5000")      # wait 5s for a lock, don't error
    cursor.execute("PRAGMA journal_mode=WAL")       # better read concurrency
    cursor.close()
    # Hand transaction control to us (disable pysqlite's automatic BEGIN).
    dbapi_connection.isolation_level = None


@event.listens_for(engine, "begin")
def _sqlite_begin_immediate(conn) -> None:
    # Acquire the write lock at the start of the transaction so two operations on
    # the same account can't both read a stale balance and then both write.
    conn.exec_driver_sql("BEGIN IMMEDIATE")


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency: one session per request, always closed."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
