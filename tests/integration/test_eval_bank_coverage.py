"""Bank coverage regression — guards the ETL ingest, not the ranker.

Skipped via ``SABLEKOL_SKIP_EVAL=1`` for fast local iteration; must run in CI.
The threshold is intentionally low so an empty gold set doesn't block CI;
operators raise the threshold once gold_set.yaml is populated.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("SABLEKOL_SKIP_EVAL") == "1",
    reason="SABLEKOL_SKIP_EVAL=1",
)


COVERAGE_THRESHOLD = float(os.environ.get("SABLEKOL_COVERAGE_THRESHOLD", "0.0"))


def test_bank_coverage_meets_threshold(db_conn, monkeypatch, tmp_path):
    """Coverage = % of gold-set rows actually present in kol_candidates."""
    from sable_kol import eval as eval_mod
    from sable_kol.db import upsert_candidate, update_classification

    # Synthetic gold set + bank for the regression-style test. The real
    # gold_set.yaml is starter-empty (operator fills it in), so we exercise the
    # API with a small in-test fixture.
    gold = tmp_path / "gold.yaml"
    gold.write_text(
        "projects:\n"
        "  - org_id: tig\n"
        "    description: 'TIG'\n"
        "    kols: ['alice', 'bob', 'carol']\n"
    )
    upsert_candidate(db_conn, handle="alice", discovery_source="cahit")
    upsert_candidate(db_conn, handle="bob", discovery_source="cahit")
    # carol intentionally missing from the bank

    @contextmanager
    def _fake_open():
        yield db_conn

    monkeypatch.setattr(eval_mod, "open_db", _fake_open)

    entries = eval_mod.load_gold_set(gold)
    coverage = eval_mod.compute_coverage(entries)
    assert len(coverage) == 1
    rep = coverage[0]
    assert rep.total_kols == 3
    assert rep.in_bank == 2
    assert "carol" in rep.missing
    assert rep.coverage == pytest.approx(2 / 3)
    assert rep.coverage >= COVERAGE_THRESHOLD


def test_real_gold_set_loads():
    """The starter gold_set.yaml must parse without error even when empty."""
    from sable_kol import eval as eval_mod
    repo_root = Path(__file__).resolve().parents[2]
    entries = eval_mod.load_gold_set(repo_root / "eval" / "gold_set.yaml")
    # Five projects scaffolded — empty kols is fine, but the structure must hold.
    assert len(entries) == 5
    labels = {e.label for e in entries}
    assert "org:tig" in labels
    assert "org:multisynq" in labels
