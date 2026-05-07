"""Gold-set evaluation harness.

Two metrics, reported and tested separately:

* **Bank coverage** — for each gold-set KOL across all projects, is there any
  live ``kol_candidates`` row at all? Reports the fraction present, broken down
  by project. This measures the ingest pipeline; the ranker is not blamed for
  missing data.

* **Ranker recall (top-N)** — of gold-set KOLs that *are* present in the bank
  for a given project, what fraction appear in the top-N results? Computed for
  N=20 and N=50. The ranker is graded only on what it had to work with.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from sable_kol.db import get_candidate_by_handle, normalize_handle, open_db
from sable_kol.match import run_find


@dataclass
class GoldSetEntry:
    label: str  # human-readable
    org_id: str | None
    external_handle: str | None
    sector: str | None
    kols: list[str]


@dataclass
class CoverageReport:
    project: str
    total_kols: int
    in_bank: int
    missing: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        return (self.in_bank / self.total_kols) if self.total_kols else 0.0


@dataclass
class RecallReport:
    project: str
    in_bank_kols: list[str]
    top20_hits: list[str] = field(default_factory=list)
    top50_hits: list[str] = field(default_factory=list)

    @property
    def recall_at_20(self) -> float:
        if not self.in_bank_kols:
            return 1.0  # vacuously true
        return len(self.top20_hits) / len(self.in_bank_kols)

    @property
    def recall_at_50(self) -> float:
        if not self.in_bank_kols:
            return 1.0
        return len(self.top50_hits) / len(self.in_bank_kols)


@dataclass
class EvalSummary:
    coverage: list[CoverageReport] = field(default_factory=list)
    recall: list[RecallReport] = field(default_factory=list)

    @property
    def overall_coverage(self) -> float:
        total = sum(c.total_kols for c in self.coverage)
        in_bank = sum(c.in_bank for c in self.coverage)
        return (in_bank / total) if total else 0.0

    @property
    def overall_recall_50(self) -> float:
        total_in_bank = sum(len(r.in_bank_kols) for r in self.recall)
        hits = sum(len(r.top50_hits) for r in self.recall)
        return (hits / total_in_bank) if total_in_bank else 1.0


# ---------------------------------------------------------------------------
# Gold set loader
# ---------------------------------------------------------------------------

def load_gold_set(path: str | Path) -> list[GoldSetEntry]:
    p = Path(path)
    raw = yaml.safe_load(p.read_text())
    out: list[GoldSetEntry] = []
    for entry in raw.get("projects", []) or []:
        kols = [normalize_handle(k) for k in (entry.get("kols") or []) if k]
        if entry.get("org_id"):
            label = f"org:{entry['org_id']}"
        else:
            label = f"external:{entry.get('external_handle')}"
        out.append(GoldSetEntry(
            label=label,
            org_id=entry.get("org_id"),
            external_handle=entry.get("external_handle"),
            sector=entry.get("sector"),
            kols=kols,
        ))
    return out


# ---------------------------------------------------------------------------
# Coverage pass — DB-only, no Anthropic calls.
# ---------------------------------------------------------------------------

def compute_coverage(entries: list[GoldSetEntry]) -> list[CoverageReport]:
    reports: list[CoverageReport] = []
    with open_db() as conn:
        for entry in entries:
            in_bank = 0
            missing: list[str] = []
            for handle in entry.kols:
                if get_candidate_by_handle(conn, handle) is not None:
                    in_bank += 1
                else:
                    missing.append(handle)
            reports.append(CoverageReport(
                project=entry.label,
                total_kols=len(entry.kols),
                in_bank=in_bank,
                missing=missing,
            ))
    return reports


# ---------------------------------------------------------------------------
# Recall pass — runs the matcher.
# ---------------------------------------------------------------------------

def compute_recall(
    entries: list[GoldSetEntry],
    *,
    haiku_client: Any | None = None,
    socialdata_fetcher: Any | None = None,
    top_n: int = 50,
) -> list[RecallReport]:
    reports: list[RecallReport] = []
    for entry in entries:
        # Skip if no gold KOLs.
        if not entry.kols:
            reports.append(RecallReport(project=entry.label, in_bank_kols=[]))
            continue
        # Compute in-bank subset first.
        with open_db() as conn:
            in_bank = [h for h in entry.kols if get_candidate_by_handle(conn, h) is not None]
        if not in_bank:
            reports.append(RecallReport(project=entry.label, in_bank_kols=[]))
            continue

        out = run_find(
            org_id=entry.org_id,
            external_handle=entry.external_handle,
            sector=entry.sector,
            limit=top_n,
            haiku_client=haiku_client,
            socialdata_fetcher=socialdata_fetcher,
            write_output=False,
        )
        result_handles = [r.handle for r in out.results]
        top20 = set(result_handles[:20])
        top50 = set(result_handles[:50])
        reports.append(RecallReport(
            project=entry.label,
            in_bank_kols=in_bank,
            top20_hits=[h for h in in_bank if h in top20],
            top50_hits=[h for h in in_bank if h in top50],
        ))
    return reports


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def run_eval(
    gold_set_path: str = "eval/gold_set.yaml",
    *,
    haiku_client: Any | None = None,
    socialdata_fetcher: Any | None = None,
) -> EvalSummary:
    """Run both passes and pretty-print a report."""
    entries = load_gold_set(gold_set_path)
    summary = EvalSummary()

    summary.coverage = compute_coverage(entries)
    print("=== Bank coverage ===")
    print(f"{'project':<32} {'covered':>10} {'total':>6}  missing")
    print("-" * 90)
    for r in summary.coverage:
        miss_excerpt = ", ".join(r.missing[:5])
        if len(r.missing) > 5:
            miss_excerpt += f" (+{len(r.missing) - 5} more)"
        print(f"{r.project:<32} {r.in_bank:>10} {r.total_kols:>6}  {miss_excerpt}")
    print(f"\nOverall coverage: {summary.overall_coverage:.1%}")

    # Skip recall when no gold-set KOLs to evaluate.
    has_kols = any(c.total_kols > 0 for c in summary.coverage)
    if not has_kols:
        print("\n(no gold-set KOLs populated — skipping recall pass)")
        summary.recall = []
        return summary

    summary.recall = compute_recall(
        entries,
        haiku_client=haiku_client,
        socialdata_fetcher=socialdata_fetcher,
    )
    print("\n=== Ranker recall (top-N over in-bank subset) ===")
    print(f"{'project':<32} {'in_bank':>8} {'r@20':>6} {'r@50':>6}")
    print("-" * 70)
    for r in summary.recall:
        print(
            f"{r.project:<32} {len(r.in_bank_kols):>8} "
            f"{r.recall_at_20:>6.2f} {r.recall_at_50:>6.2f}"
        )
    print(f"\nOverall recall@50: {summary.overall_recall_50:.1%}")
    return summary
