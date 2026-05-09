"""
One-shot migrator: copy KOL tables from a local SQLite sable.db into prod Postgres.

Reason this exists: SableKOL's Phase 0-8 work landed in the operator's local
~/.sable/sable.db (SQLite). Prod /opt/sable/.env points at Postgres, where the
schema is at head=039 but the kol_* tables are empty. Running `regenerate` on
prod produced a graph with 0 nodes/0 edges. This script ports the data over.

Usage on the box (after scp'ing the local sqlite up):
    set -a; . /opt/sable/.env; set +a
    /opt/sable/venv/bin/python /opt/sable/sable-kol/deploy/migrate_kol_to_pg.py /tmp/sable_local.db

Idempotent: every INSERT uses ON CONFLICT DO NOTHING. Re-running is a no-op.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values

# (table, conflict_columns, sequence_pk_column or None).
# Order matters: kol_handle_resolution_conflicts FK-references kol_candidates;
# kol_follow_edges FK-references kol_extract_runs.
TABLES: list[tuple[str, list[str], str | None]] = [
    ("kol_candidates", ["candidate_id"], "candidate_id"),
    ("kol_extract_runs", ["run_id"], None),  # run_id is TEXT (UUID)
    ("kol_handle_resolution_conflicts", ["conflict_id"], "conflict_id"),
    ("kol_follow_edges", ["run_id", "follower_id", "followed_id"], None),
    ("kol_operator_relationships", ["id"], "id"),
]

BATCH = 5000


def fetch_columns(sqlite_conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in sqlite_conn.execute(f"PRAGMA table_info({table})")]


def copy_table(
    src: sqlite3.Connection,
    dst,
    table: str,
    conflict_cols: list[str],
) -> int:
    cols = fetch_columns(src, table)
    if not cols:
        return 0

    insert_template = sql.SQL(
        "INSERT INTO {tbl} ({cols}) VALUES %s ON CONFLICT ({conflict}) DO NOTHING"
    ).format(
        tbl=sql.Identifier(table),
        cols=sql.SQL(", ").join(map(sql.Identifier, cols)),
        conflict=sql.SQL(", ").join(map(sql.Identifier, conflict_cols)),
    )

    cur_src = src.execute(f"SELECT {', '.join(cols)} FROM {table}")
    inserted = 0
    with dst.cursor() as cur_dst:
        batch: list[tuple] = []
        for row in cur_src:
            batch.append(tuple(row))
            if len(batch) >= BATCH:
                execute_values(cur_dst, insert_template.as_string(dst), batch, page_size=BATCH)
                inserted += len(batch)
                batch = []
                print(f"  {table}: {inserted:,} rows...", flush=True)
        if batch:
            execute_values(cur_dst, insert_template.as_string(dst), batch, page_size=BATCH)
            inserted += len(batch)
    dst.commit()
    return inserted


def bump_sequence(dst, table: str, pk_col: str) -> None:
    with dst.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT setval(pg_get_serial_sequence(%s, %s), "
                "COALESCE((SELECT MAX({pk}) FROM {tbl}), 1), true)"
            ).format(pk=sql.Identifier(pk_col), tbl=sql.Identifier(table)),
            (table, pk_col),
        )
    dst.commit()


def count(conn, table: str, *, sqlite_mode: bool) -> int:
    if sqlite_mode:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT COUNT(*) FROM {tbl}").format(tbl=sql.Identifier(table)))
        return cur.fetchone()[0]


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: migrate_kol_to_pg.py <path-to-sqlite-db>", file=sys.stderr)
        return 2
    sqlite_path = sys.argv[1]
    pg_url = os.environ.get("SABLE_DATABASE_URL")
    if not pg_url:
        print("SABLE_DATABASE_URL not set in env", file=sys.stderr)
        return 2

    print(f"src: {sqlite_path}")
    print(f"dst: {pg_url.rsplit('@', 1)[-1]}")

    src = sqlite3.connect(sqlite_path)
    dst = psycopg2.connect(pg_url)

    try:
        for table, conflict_cols, pk_seq in TABLES:
            n_src = count(src, table, sqlite_mode=True)
            n_dst_before = count(dst, table, sqlite_mode=False)
            print(f"\n{table}: sqlite={n_src:,}  postgres-before={n_dst_before:,}")
            if n_src == 0:
                print(f"  {table}: source empty, skipping")
                continue
            copy_table(src, dst, table, conflict_cols)
            n_dst_after = count(dst, table, sqlite_mode=False)
            print(f"  {table}: postgres-after={n_dst_after:,} "
                  f"(net +{n_dst_after - n_dst_before:,})")
            if pk_seq:
                bump_sequence(dst, table, pk_seq)
                print(f"  {table}: sequence bumped on {pk_seq}")
    finally:
        src.close()
        dst.close()

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
