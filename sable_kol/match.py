"""Hybrid scorer + Haiku rationales — the matcher.

Pipeline per ``run_find``:

1. Build the project profile (path i or ii).
2. Rule pre-rank: weighted-sum score over candidate signals, top **K=30**.
3. Haiku rationale per top-K candidate under a strict evidence contract.
4. Cost-event logging for every Anthropic call.
5. Output canonical JSON (or pretty terminal table).

The evidence contract is **enforced**, not just warned. Haiku must return
``used_evidence_keys`` listing dotted-path keys it cites; any unknown key OR
any hit on the fabrication denylist forces a regenerate. Persistent violations
fall back to the rule-prerank score with a placeholder rationale.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from sable_kol import cost as cost_mod
from sable_kol.db import Candidate, list_candidates, open_db
from sable_kol.profile import (
    Profile,
    build_external_profile,
    build_org_profile,
)


logger = logging.getLogger(__name__)

# Per-run defaults.
TOP_K = 30
DEFAULT_RATIONALE_MODEL = "claude-haiku-4-5-20251001"

# Pricing — for cost estimation. Same as classify.py.
_HAIKU_INPUT_USD_PER_1M = 1.0
_HAIKU_OUTPUT_USD_PER_1M = 5.0


# ---------------------------------------------------------------------------
# Score weights — sum should be 1.0 for interpretable [0,100] output.
# ---------------------------------------------------------------------------

WEIGHTS = {
    "sector_overlap":    0.25,
    "archetype_match":   0.15,
    "bio_keyword_sim":   0.15,
    "sable_relationship":0.25,
    "centrality_proxy":  0.10,
    "kol_strength":      0.10,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9


# Archetypes the matcher prefers in Phase 0 — thought leaders + connectors.
PREFERRED_ARCHETYPES = {"thought_leader", "connector", "researcher"}


# Fabrication denylist — phrases Haiku should NEVER produce in Phase 0
# because the corresponding signals don't exist yet.
FABRICATION_DENYLIST = [
    "reply rate", "reply-rate",
    "engagement rate", "engagement-rate",
    "recent tweet", "recent tweets",
    "active poster",
    "posts frequently", "frequent poster",
    "high activity", "highly active",
    "thread game",
    "alpha caller",
    "trending",
]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

@dataclass
class SignalBreakdown:
    sector_overlap: float = 0.0
    archetype_match: float = 0.0
    bio_keyword_sim: float = 0.0
    sable_relationship: float = 0.0
    centrality_proxy: float = 0.0
    kol_strength: float = 0.0

    def weighted_total(self) -> float:
        return (
            WEIGHTS["sector_overlap"]    * self.sector_overlap +
            WEIGHTS["archetype_match"]   * self.archetype_match +
            WEIGHTS["bio_keyword_sim"]   * self.bio_keyword_sim +
            WEIGHTS["sable_relationship"]* self.sable_relationship +
            WEIGHTS["centrality_proxy"]  * self.centrality_proxy +
            WEIGHTS["kol_strength"]      * self.kol_strength
        )


def score_candidate(c: Candidate, p: Profile) -> SignalBreakdown:
    """Rule-based score in [0,1] per signal."""
    bd = SignalBreakdown()

    # Sector overlap: |c.sector_tags ∩ p.sectors| / max(1, |p.sectors|)
    proj_sectors = {s.lower() for s in p.sectors if s}
    cand_sectors = {s.lower() for s in c.sector_tags}
    if proj_sectors:
        bd.sector_overlap = len(cand_sectors & proj_sectors) / len(proj_sectors)
    else:
        bd.sector_overlap = 0.0

    # Archetype match: 1.0 if any preferred archetype, 0.5 if any classified, else 0.
    cand_arch = set(c.archetype_tags)
    if cand_arch & PREFERRED_ARCHETYPES:
        bd.archetype_match = 1.0
    elif cand_arch:
        bd.archetype_match = 0.5
    else:
        bd.archetype_match = 0.0

    # Bio keyword similarity: simple bag-of-words overlap with project themes
    # + voice_blob keywords.
    project_kw = _project_keywords(p)
    cand_kw = _tokenize(c.bio_snapshot or "")
    if project_kw and cand_kw:
        overlap = len(project_kw & cand_kw)
        bd.bio_keyword_sim = min(1.0, overlap / 5.0)  # 5 hits = max
    else:
        bd.bio_keyword_sim = 0.0

    # Sable relationship: 1.0 if any community match or operator follow,
    # 0.0 otherwise.
    rel = c.sable_relationship or {"communities": [], "operators": []}
    has_community = bool(rel.get("communities"))
    has_operator = bool(rel.get("operators"))
    if has_community and has_operator:
        bd.sable_relationship = 1.0
    elif has_community or has_operator:
        bd.sable_relationship = 0.7
    else:
        bd.sable_relationship = 0.0

    # Centrality proxy: # of org-prefixed discovery sources, capped at 3.
    org_sources = sum(1 for s in c.discovery_sources if s.startswith("org:"))
    bd.centrality_proxy = min(1.0, org_sources / 3.0)

    # KOL strength: prefer the stored kol_strength_score (computed by `enrich`).
    # Fall back to on-the-fly computation when the bank hasn't been enriched yet
    # so `find` still works pre-enrichment, just with weaker signal.
    stored = getattr(c, "kol_strength_score", None)
    if stored is not None:
        bd.kol_strength = max(0.0, min(1.0, float(stored)))
    else:
        from sable_kol.enrich import compute_kol_strength
        bd.kol_strength = compute_kol_strength(c)

    return bd


_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "on", "in", "to", "for",
    "with", "is", "are", "was", "were", "be", "been", "by", "as", "at",
    "this", "that", "these", "those", "it", "its", "from", "we", "us",
    "they", "them", "our", "your", "their", "i", "you", "he", "she",
    "crypto", "twitter",  # too generic in this domain
}


def _tokenize(text: str) -> set[str]:
    return {
        w.lower() for w in _TOKEN_RE.findall(text)
        if w.lower() not in _STOPWORDS and len(w) > 2
    }


def _project_keywords(p: Profile) -> set[str]:
    parts: list[str] = []
    parts.extend(p.themes or [])
    parts.extend(p.top_tags or [])
    if p.voice_blob:
        parts.append(p.voice_blob)
    if p.sector:
        parts.append(p.sector)
    return _tokenize(" ".join(parts))


# ---------------------------------------------------------------------------
# Evidence contract
# ---------------------------------------------------------------------------

def _build_candidate_evidence(c: Candidate, score: SignalBreakdown) -> dict:
    return {
        "twitter_id": c.twitter_id,
        "handle": c.handle_normalized,
        "display_name": c.display_name,
        "bio_snapshot": c.bio_snapshot,
        "followers": c.followers_snapshot,
        "archetype_tags": c.archetype_tags,
        "sector_tags": c.sector_tags,
        "sable_relationship": c.sable_relationship,
        "discovery_sources": c.discovery_sources,
        "signal_breakdown": asdict(score),
    }


def _validate_used_keys(used: list[str], evidence: dict) -> list[str]:
    """Return any used keys that don't resolve in the evidence dict."""
    bad = []
    for key in used:
        if not _key_present(key, evidence):
            bad.append(key)
    return bad


def _key_present(dotted: str, evidence: dict) -> bool:
    parts = dotted.split(".")
    cur: Any = evidence
    for p in parts:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return False
    return True


def _has_fabrication_phrase(rationale: str) -> str | None:
    text = rationale.lower()
    for phrase in FABRICATION_DENYLIST:
        if phrase in text:
            return phrase
    return None


# ---------------------------------------------------------------------------
# Haiku call
# ---------------------------------------------------------------------------

RATIONALE_SYSTEM_PROMPT = """You are evaluating whether a Crypto Twitter (CT) account is a good KOL fit for a project.

You receive:
- project_profile: the project under consideration
- candidate_evidence: ALL the data we have about the candidate

Return strict JSON with these keys:
  - score: integer 0-100 (your overall fit assessment)
  - rationale: ONE sentence explaining the score, citing only fields from candidate_evidence
  - used_evidence_keys: array of dotted-path keys from candidate_evidence that your rationale references
                       (e.g., "sector_tags", "sable_relationship.communities", "archetype_tags")

CRITICAL RULES:
- You may ONLY cite signals that exist in candidate_evidence. You must NOT invent signals like
  "reply rate", "engagement rate", "recent tweets", "active poster", "frequent poster",
  "alpha caller", "thread game", or any time-based or activity-based metric — none of those
  are in candidate_evidence in this phase.
- If you have no strong signal, return rationale: "No strong signal beyond sector/archetype match."
  with used_evidence_keys: ["sector_tags", "archetype_tags"] (or whatever you actually used).
- Reply ONLY with JSON. No prose, no markdown fences.
"""


def _call_haiku_rationale(
    client,
    *,
    project_profile: dict,
    candidate_evidence: dict,
    model: str,
    extra_instruction: str | None = None,
) -> tuple[dict, int, int]:
    """One Haiku call. Returns (parsed_response, input_tokens, output_tokens)."""
    user_msg = (
        "project_profile:\n"
        + json.dumps(project_profile, ensure_ascii=False)
        + "\n\ncandidate_evidence:\n"
        + json.dumps(candidate_evidence, ensure_ascii=False, default=str)
    )
    if extra_instruction:
        user_msg += "\n\n" + extra_instruction
    resp = client.messages.create(
        model=model,
        max_tokens=400,
        system=RATIONALE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    parsed = json.loads(text)
    return parsed, resp.usage.input_tokens, resp.usage.output_tokens


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class MatchResult:
    twitter_id: str | None
    handle: str
    display_name: str | None
    followers: int | None
    score: float
    rule_prerank_score: float
    signal_breakdown: dict
    candidate_evidence: dict
    rationale: str
    used_evidence_keys: list[str] = field(default_factory=list)
    sable_context: dict = field(default_factory=dict)
    enrichment_tier: str = "none"


@dataclass
class FindOutput:
    project: dict
    results: list[MatchResult]
    cost_usd: float = 0.0
    candidates_considered: int = 0
    k_evaluated: int = 0
    evidence_violations: int = 0
    warnings: list[str] = field(default_factory=list)
    generated_at: str = ""


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_find(
    *,
    org_id: str | None = None,
    external_handle: str | None = None,
    sector: str | None = None,
    themes: list[str] | None = None,
    paid_enrich: bool = False,
    refresh_paid: bool = False,
    limit: int = 20,
    output_format: str = "table",
    top_k: int = TOP_K,
    rationale_model: str = DEFAULT_RATIONALE_MODEL,
    haiku_client=None,
    socialdata_fetcher=None,
    write_output: bool = True,
) -> FindOutput:
    """Run the matcher end-to-end. Returns a FindOutput regardless of output_format.

    ``haiku_client`` and ``socialdata_fetcher`` are injectable for tests.
    """
    with open_db() as conn:
        # Build project profile.
        if org_id:
            profile = build_org_profile(conn, org_id)
        else:
            assert external_handle and sector
            profile = build_external_profile(
                conn,
                handle=external_handle,
                sector=sector,
                themes=themes or [],
                paid_enrich=paid_enrich,
                refresh_paid=refresh_paid,
                socialdata_fetcher=socialdata_fetcher,
            )

        # Fetch live, classified, non-dropped candidates.
        all_active = [
            c for c in list_candidates(conn, status="active", only_classified=True)
            if c.archetype_tags  # double-check classifier ran
        ]
        candidates_considered = len(all_active)

        # Rule pre-rank.
        scored = [(c, score_candidate(c, profile)) for c in all_active]
        scored.sort(key=lambda x: x[1].weighted_total(), reverse=True)
        topk = scored[:top_k]

        # Haiku rationales with evidence-contract enforcement.
        results: list[MatchResult] = []
        warnings: list[str] = []
        evidence_violations = 0
        cost_usd = 0.0

        if topk and haiku_client is None:
            haiku_client = _build_anthropic_client()

        cost_org = profile.org_id  # None → _external sentinel

        project_evidence = profile.to_evidence_dict()
        # Voice blob is included as a separate field so it doesn't bloat the
        # key-validation surface.
        if profile.voice_blob:
            project_evidence["voice_blob_excerpt"] = profile.voice_blob[:1500]

        for c, score in topk:
            rule_prerank = round(score.weighted_total() * 100, 1)
            evidence = _build_candidate_evidence(c, score)
            try:
                rationale, used, in_t, out_t, violation_count = _rationale_with_retry(
                    haiku_client,
                    project_profile=project_evidence,
                    candidate_evidence=evidence,
                    model=rationale_model,
                )
            except Exception as exc:
                warnings.append(f"haiku-call-failed:{c.handle_normalized}:{exc}")
                rationale = "<haiku call failed>"
                used = []
                in_t = out_t = 0
                violation_count = 0
                cost_mod.record(
                    conn,
                    org_id=cost_org,
                    call_type="anthropic_haiku_rationale",
                    cost_usd=0.0,
                    model=rationale_model,
                    call_status="error",
                )

            if in_t or out_t:
                call_cost = (
                    in_t * _HAIKU_INPUT_USD_PER_1M / 1_000_000
                    + out_t * _HAIKU_OUTPUT_USD_PER_1M / 1_000_000
                )
                cost_usd += call_cost
                cost_mod.record(
                    conn,
                    org_id=cost_org,
                    call_type="anthropic_haiku_rationale",
                    cost_usd=call_cost,
                    model=rationale_model,
                    input_tokens=in_t,
                    output_tokens=out_t,
                )

            evidence_violations += violation_count
            if violation_count >= 2:
                warnings.append(
                    f"evidence-violation:{c.handle_normalized}:degraded-to-prerank"
                )

            sable_context = {
                "in_client_communities": [
                    com["org_id"]
                    for com in (c.sable_relationship or {}).get("communities", [])
                ],
                "followed_by_operators": [
                    op["name"]
                    for op in (c.sable_relationship or {}).get("operators", [])
                ],
            }

            # Use the Haiku score when its rationale is valid; else rule prerank.
            llm_score = _coerce_score(rationale)
            final_score = (
                rule_prerank
                if violation_count >= 2 or llm_score is None
                else float(llm_score)
            )

            rationale_text = (
                "<excluded due to evidence violation>"
                if violation_count >= 2
                else _coerce_rationale(rationale)
            )

            results.append(MatchResult(
                twitter_id=c.twitter_id,
                handle=c.handle_normalized,
                display_name=c.display_name,
                followers=c.followers_snapshot,
                score=final_score,
                rule_prerank_score=rule_prerank,
                signal_breakdown=asdict(score),
                candidate_evidence=evidence,
                rationale=rationale_text,
                used_evidence_keys=used,
                sable_context=sable_context,
                enrichment_tier=c.enrichment_tier,
            ))

        # Sort by final score and trim to limit.
        results.sort(key=lambda r: r.score, reverse=True)
        results = results[:limit]

        out = FindOutput(
            project=project_evidence,
            results=results,
            cost_usd=round(cost_usd, 6),
            candidates_considered=candidates_considered,
            k_evaluated=len(topk),
            evidence_violations=evidence_violations,
            warnings=warnings,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

    if write_output:
        if output_format == "json":
            print(json.dumps(_find_output_to_json(out), indent=2, default=str))
        else:
            _print_table(out)

    return out


def _rationale_with_retry(
    client,
    *,
    project_profile: dict,
    candidate_evidence: dict,
    model: str,
) -> tuple[dict, list[str], int, int, int]:
    """Returns (parsed_dict, used_keys, total_input_tokens, total_output_tokens, violation_count).

    On the first response, validate. If used_evidence_keys contains unknown
    keys OR rationale hits the fabrication denylist, retry ONCE with a
    correction prompt. On second failure, return violation_count=2.
    """
    parsed, in_t, out_t = _call_haiku_rationale(
        client,
        project_profile=project_profile,
        candidate_evidence=candidate_evidence,
        model=model,
    )
    used = list(parsed.get("used_evidence_keys") or [])
    rationale = _coerce_rationale(parsed)
    bad_keys = _validate_used_keys(used, candidate_evidence)
    bad_phrase = _has_fabrication_phrase(rationale)
    if not bad_keys and not bad_phrase:
        return parsed, used, in_t, out_t, 0

    # One retry.
    correction = (
        "Your previous response was rejected by the evidence contract:\n"
    )
    if bad_keys:
        correction += f"- used_evidence_keys contained keys not in candidate_evidence: {bad_keys}\n"
    if bad_phrase:
        correction += (
            f"- rationale contained the disallowed fabrication phrase '{bad_phrase}'. "
            f"That signal is NOT in candidate_evidence.\n"
        )
    correction += (
        "Regenerate using ONLY keys present in candidate_evidence. "
        "If you have no strong signal, say so explicitly."
    )
    parsed2, in2, out2 = _call_haiku_rationale(
        client,
        project_profile=project_profile,
        candidate_evidence=candidate_evidence,
        model=model,
        extra_instruction=correction,
    )
    used2 = list(parsed2.get("used_evidence_keys") or [])
    rationale2 = _coerce_rationale(parsed2)
    bad_keys2 = _validate_used_keys(used2, candidate_evidence)
    bad_phrase2 = _has_fabrication_phrase(rationale2)
    if not bad_keys2 and not bad_phrase2:
        return parsed2, used2, in_t + in2, out_t + out2, 1
    return parsed2, used2, in_t + in2, out_t + out2, 2


def _coerce_score(parsed: dict | None) -> float | None:
    if not parsed:
        return None
    s = parsed.get("score")
    if isinstance(s, (int, float)):
        return float(max(0, min(100, s)))
    return None


def _coerce_rationale(parsed: dict | None) -> str:
    if not parsed:
        return ""
    r = parsed.get("rationale")
    return str(r) if r is not None else ""


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _find_output_to_json(out: FindOutput) -> dict:
    return {
        "project": out.project,
        "results": [
            {
                "twitter_id": r.twitter_id,
                "handle": r.handle,
                "display_name": r.display_name,
                "followers": r.followers,
                "score": r.score,
                "rule_prerank_score": r.rule_prerank_score,
                "signal_breakdown": r.signal_breakdown,
                "candidate_evidence": r.candidate_evidence,
                "rationale": r.rationale,
                "used_evidence_keys": r.used_evidence_keys,
                "sable_context": r.sable_context,
                "enrichment_tier": r.enrichment_tier,
            }
            for r in out.results
        ],
        "query_metadata": {
            "cost_usd": out.cost_usd,
            "candidates_considered": out.candidates_considered,
            "k_evaluated": out.k_evaluated,
            "evidence_violations": out.evidence_violations,
            "warnings": out.warnings,
            "generated_at": out.generated_at,
        },
    }


def _print_table(out: FindOutput) -> None:
    print(
        f"\nProject: {out.project.get('source')}  "
        f"[org_id={out.project.get('org_id')}, sector={out.project.get('sector')}, "
        f"sectors={out.project.get('sectors')}]"
    )
    print(
        f"Considered {out.candidates_considered} candidates, "
        f"evaluated top-{out.k_evaluated}, "
        f"cost ${out.cost_usd:.4f}, "
        f"evidence violations: {out.evidence_violations}\n"
    )
    print(f"{'#':>3}  {'score':>5}  {'handle':<22} {'rationale'}")
    print("-" * 100)
    for i, r in enumerate(out.results, 1):
        rat = (r.rationale or "")[:80]
        print(f"{i:>3}  {r.score:>5.1f}  @{r.handle:<21} {rat}")
    if out.warnings:
        print("\nwarnings:")
        for w in out.warnings:
            print(f"  - {w}")


def _build_anthropic_client():
    import anthropic
    from sable_kol.config import resolve_anthropic_api_key
    api_key = resolve_anthropic_api_key()
    if not api_key:
        raise RuntimeError(
            "Anthropic API key not found. Set ANTHROPIC_API_KEY env var "
            "or add `anthropic_api_key: ...` to ~/.sable/config.yaml."
        )
    return anthropic.Anthropic(api_key=api_key)
