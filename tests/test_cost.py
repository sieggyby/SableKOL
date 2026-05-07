"""Tests for sable_kol.cost."""
from __future__ import annotations

from sable_kol.cost import EXTERNAL_ORG_ID, record


def test_record_writes_event_and_creates_sentinel_org(db_conn):
    record(
        db_conn,
        org_id=None,
        call_type="anthropic_haiku_classify",
        cost_usd=0.0012,
        model="claude-haiku-4-5",
        input_tokens=200,
        output_tokens=80,
    )
    org_row = db_conn.execute(
        "SELECT * FROM orgs WHERE org_id = :id", {"id": EXTERNAL_ORG_ID}
    ).fetchone()
    assert org_row is not None
    assert org_row["status"] == "active"

    ev = db_conn.execute(
        "SELECT * FROM cost_events WHERE org_id = :id", {"id": EXTERNAL_ORG_ID}
    ).fetchone()
    assert ev is not None
    assert ev["call_type"] == "sablekol.anthropic_haiku_classify"
    assert ev["model"] == "claude-haiku-4-5"
    assert ev["input_tokens"] == 200
    assert ev["output_tokens"] == 80
    assert abs(ev["cost_usd"] - 0.0012) < 1e-9


def test_record_uses_real_org_when_provided(db_conn):
    db_conn.execute(
        "INSERT INTO orgs (org_id, display_name, status) VALUES ('tig', 'TIG', 'active')"
    )
    db_conn.commit()
    record(
        db_conn,
        org_id="tig",
        call_type="socialdata_user_profile",
        cost_usd=0.002,
    )
    ev = db_conn.execute(
        "SELECT * FROM cost_events WHERE org_id = 'tig'"
    ).fetchone()
    assert ev is not None
    assert ev["call_type"] == "sablekol.socialdata_user_profile"


def test_record_call_status_can_be_set(db_conn):
    record(
        db_conn,
        org_id=None,
        call_type="anthropic_haiku_rationale",
        cost_usd=0.0,
        call_status="error",
    )
    ev = db_conn.execute(
        "SELECT call_status FROM cost_events WHERE call_type = 'sablekol.anthropic_haiku_rationale'"
    ).fetchone()
    assert ev["call_status"] == "error"
