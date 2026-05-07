"""Test fixtures for SableKOL.

``db_conn`` mirrors SablePlatform's CompatConnection-backed test fixture so that
production code paths (which use SQLAlchemy ``text()``-wrapped SQL via
``log_cost`` etc.) work the same way under test as in production. Schema is
created via ``ensure_schema`` over the underlying DBAPI connection so the SQL
migration path is exercised in tests too.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event

from sable_platform.db.compat_conn import CompatConnection


@pytest.fixture
def db_conn():
    """Fresh in-memory sable.db (CompatConnection)."""
    from sable_platform.db.connection import ensure_schema

    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, connection_record):  # noqa: ARG001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    raw_proxy = engine.raw_connection()
    try:
        dbapi_conn = raw_proxy.dbapi_connection
        import sqlite3
        dbapi_conn.row_factory = sqlite3.Row
        ensure_schema(dbapi_conn)
    finally:
        raw_proxy.close()

    sa_conn = engine.connect()
    conn = CompatConnection(sa_conn)
    yield conn
    conn.close()
    engine.dispose()
