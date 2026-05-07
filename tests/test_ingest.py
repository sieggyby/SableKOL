"""Tests for ingest parsing + driver."""
from __future__ import annotations

import json
from pathlib import Path

from sable_kol.ingest import (
    parse_html_export,
    parse_json_export,
    parse_export,
)


# ---------------------------------------------------------------------------
# JSON parser
# ---------------------------------------------------------------------------

def test_json_parser_handles_canonical_keys():
    text = json.dumps([
        {"handle": "alice", "display_name": "Alice", "bio": "DeFi", "followers": 1234},
        {"handle": "@Bob", "name": "Bob", "description": "Solana", "followers_count": 5678},
        {"username": "carol"},
    ])
    parsed = parse_json_export(text)
    assert len(parsed) == 3
    assert parsed[0].handle == "alice"
    assert parsed[0].followers == 1234
    assert parsed[1].handle == "@Bob"
    assert parsed[1].display_name == "Bob"
    assert parsed[1].bio == "Solana"
    assert parsed[1].followers == 5678
    assert parsed[2].handle == "carol"
    assert parsed[2].display_name is None


def test_json_parser_skips_malformed_rows():
    text = json.dumps([
        {"handle": "valid"},
        "not a dict",
        {"no_handle_field": "x"},
        42,
    ])
    parsed = parse_json_export(text)
    assert len(parsed) == 1
    assert parsed[0].handle == "valid"


# ---------------------------------------------------------------------------
# HTML parser
# ---------------------------------------------------------------------------

def test_html_parser_extracts_handle_anchors():
    html = """
    <html><body>
      <a href="/alice">Alice</a>
      <a href="/bob_eth">Bob Eth</a>
      <a href="/carol/status/123">noise</a>
      <a href="/home">noise</a>
      <a href="/i/lists/123">noise</a>
      <a href="/dave">@dave</a>  <!-- text matches handle, no display name -->
    </body></html>
    """
    parsed = parse_html_export(html)
    handles = sorted(p.handle for p in parsed)
    assert handles == ["alice", "bob_eth", "dave"]
    by_handle = {p.handle: p for p in parsed}
    assert by_handle["alice"].display_name == "Alice"
    assert by_handle["bob_eth"].display_name == "Bob Eth"
    assert by_handle["dave"].display_name is None


def test_html_parser_dedupes_handles():
    """Same handle appearing in multiple anchors collapses to one row."""
    html = """
    <a href="/eve"></a>
    <a href="/eve">Eve Display</a>
    <a href="/eve">extra</a>
    """
    parsed = parse_html_export(html)
    assert len(parsed) == 1
    assert parsed[0].handle == "eve"
    assert parsed[0].display_name == "Eve Display"


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def test_parse_export_routes_by_extension(tmp_path: Path):
    json_file = tmp_path / "cahit.json"
    json_file.write_text(json.dumps([{"handle": "alice"}]))
    html_file = tmp_path / "cahit.html"
    html_file.write_text("<a href='/bob'>Bob</a>")

    parsed_json = parse_export(json_file)
    parsed_html = parse_export(html_file)
    assert {p.handle for p in parsed_json} == {"alice"}
    assert {p.handle for p in parsed_html} == {"bob"}


def test_parse_export_sniffs_content_when_extension_missing(tmp_path: Path):
    f = tmp_path / "noext"
    f.write_text(json.dumps([{"handle": "z"}]))
    parsed = parse_export(f)
    assert parsed[0].handle == "z"


# ---------------------------------------------------------------------------
# Driver — uses real DB through monkeypatch of open_db
# ---------------------------------------------------------------------------

def test_run_ingest_writes_rows(tmp_path: Path, db_conn, monkeypatch):
    """run_ingest() must call upsert_candidate for each parsed row."""
    from contextlib import contextmanager
    from sable_kol import ingest as ingest_mod

    @contextmanager
    def _fake_open():
        yield db_conn

    monkeypatch.setattr(ingest_mod, "open_db", _fake_open)

    f = tmp_path / "list.json"
    f.write_text(json.dumps([
        {"handle": "alice", "display_name": "Alice", "followers": 1000},
        {"handle": "bob"},
    ]))
    summary = ingest_mod.run_ingest(str(f), source_id="cahit_list")
    assert summary.parsed == 2
    assert summary.inserted == 2
    assert summary.updated == 0
    assert summary.conflicts == 0

    rows = db_conn.execute(
        "SELECT handle_normalized FROM kol_candidates WHERE is_unresolved=0 ORDER BY handle_normalized"
    ).fetchall()
    assert [r["handle_normalized"] for r in rows] == ["alice", "bob"]


def test_run_ingest_is_idempotent(tmp_path: Path, db_conn, monkeypatch):
    from contextlib import contextmanager
    from sable_kol import ingest as ingest_mod

    @contextmanager
    def _fake_open():
        yield db_conn

    monkeypatch.setattr(ingest_mod, "open_db", _fake_open)

    f = tmp_path / "list.json"
    f.write_text(json.dumps([{"handle": "alice"}]))
    s1 = ingest_mod.run_ingest(str(f), source_id="cahit_list")
    s2 = ingest_mod.run_ingest(str(f), source_id="cahit_list")
    assert s1.inserted == 1
    # Second run finds the same handle with same source — updates without
    # adding a duplicate source entry.
    assert s2.updated == 1
    assert s2.inserted == 0

    row = db_conn.execute(
        "SELECT discovery_sources_json FROM kol_candidates WHERE handle_normalized='alice'"
    ).fetchone()
    sources = json.loads(row["discovery_sources_json"])
    assert sources == ["cahit_list"]
