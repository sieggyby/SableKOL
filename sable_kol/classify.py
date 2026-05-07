"""ETL Stage 3 — Haiku archetype + sector classifier.

For each unclassified ``kol_candidates`` row, ask Haiku to fill in:

  * ``archetype_tags``  ⊆ {thought_leader, connector, dev, anon, founder, researcher, ecosystem}
  * ``sector_tags``     ⊆ {defi, gaming, infra, ai, desci, memes, nfts, l2_eth, sol, btc, social, other}
  * ``status``          ∈ {active, low_signal, drop}

Bulk-batchable — one Anthropic call per group of N candidates (default N=20). The
prompt asks for a JSON object keyed by handle.

Cost: ~$0.001 per candidate at Haiku 4.5 prices. 1500 candidates ≈ $1.50 one-shot.

Every batch call writes a row to ``cost_events`` via ``sable_kol.cost.record``.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

from sable_kol.cost import record as cost_record
from sable_kol.db import (
    Candidate,
    list_unclassified,
    open_db,
    update_classification,
)


logger = logging.getLogger(__name__)

# Default model for classification. Cheap, fast, structured-output-friendly.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

VALID_ARCHETYPES = {
    "thought_leader", "connector", "dev", "anon",
    "founder", "researcher", "ecosystem", "trader",
}
VALID_SECTORS = {
    "defi", "gaming", "infra", "ai", "desci", "memes",
    "nfts", "l2_eth", "sol", "btc", "social", "other",
}
VALID_STATUSES = {"active", "low_signal", "drop"}

CLASSIFY_BATCH_SIZE = 20

# Haiku 4.5 pricing (per 1M tokens). Used to compute cost from token counts.
# Source: claude.ai/pricing as of writing — adjust if pricing changes.
_HAIKU_INPUT_USD_PER_1M = 1.0
_HAIKU_OUTPUT_USD_PER_1M = 5.0


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

CLASSIFY_SYSTEM_PROMPT = """You are an expert classifier of crypto-Twitter (CT) accounts.

For each input account, return strict JSON with these keys:
  - archetype_tags: array of 1-3 tags from this set:
      thought_leader, connector, dev, anon, founder, researcher, ecosystem, trader
  - sector_tags: array of 1-3 tags from this set:
      defi, gaming, infra, ai, desci, memes, nfts, l2_eth, sol, btc, social, other
  - status: one of: active, low_signal, drop
      * "drop" — obvious bot, scammer, OnlyFans/spam, dead account
      * "low_signal" — real but low-value (vague bio, no clear domain)
      * "active" — anything else

Be conservative — when in doubt, prefer "active". Only "drop" obvious noise.

Reply ONLY with JSON. No prose. Top-level shape:
  { "<handle>": { "archetype_tags": [...], "sector_tags": [...], "status": "..." }, ... }
"""


def _candidate_to_input(c: Candidate) -> dict:
    return {
        "handle": c.handle_normalized,
        "display_name": c.display_name or "",
        "bio": (c.bio_snapshot or "")[:400],
    }


def _build_user_message(batch: list[Candidate]) -> str:
    items = [_candidate_to_input(c) for c in batch]
    return (
        "Classify the following accounts. Reply with a single JSON object keyed by handle.\n\n"
        + json.dumps(items, ensure_ascii=False)
    )


# ---------------------------------------------------------------------------
# Haiku call
# ---------------------------------------------------------------------------

@dataclass
class ClassifyResponse:
    by_handle: dict[str, dict] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0


def _call_haiku(client, batch: list[Candidate], model: str) -> ClassifyResponse:
    user_msg = _build_user_message(batch)
    resp = client.messages.create(
        model=model,
        max_tokens=2000,
        system=CLASSIFY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    by_handle = _parse_json_object(text)
    return ClassifyResponse(
        by_handle=by_handle,
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
    )


def _parse_json_object(text: str) -> dict:
    """Extract a JSON object from a possibly-fenced response."""
    text = text.strip()
    if text.startswith("```"):
        # Strip markdown fences
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        # Last-ditch: find first { and last } and try
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            obj = json.loads(text[start : end + 1])
        else:
            raise
    if not isinstance(obj, dict):
        raise ValueError("Classifier response was not a JSON object")
    return obj


def _validate_classification(raw: dict) -> tuple[list[str], list[str], str]:
    """Return (archetype_tags, sector_tags, status). Coerce/clamp to valid sets."""
    archetypes_raw = raw.get("archetype_tags") or []
    sectors_raw = raw.get("sector_tags") or []
    status = raw.get("status") or "active"
    if status not in VALID_STATUSES:
        status = "active"
    archetypes = [t for t in archetypes_raw if t in VALID_ARCHETYPES]
    sectors = [t for t in sectors_raw if t in VALID_SECTORS]
    return archetypes, sectors, status


def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens * _HAIKU_INPUT_USD_PER_1M / 1_000_000
        + output_tokens * _HAIKU_OUTPUT_USD_PER_1M / 1_000_000
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

@dataclass
class ClassifySummary:
    classified: int = 0
    dropped: int = 0
    cost_usd: float = 0.0
    batches: int = 0
    errors: int = 0


def run_classify(
    *,
    limit: int | None = None,
    force: bool = False,
    model: str = DEFAULT_MODEL,
    batch_size: int = CLASSIFY_BATCH_SIZE,
    client=None,
) -> ClassifySummary:
    """Classify pending rows. Returns a summary."""
    summary = ClassifySummary()

    with open_db() as conn:
        if force:
            from sable_kol.db import list_candidates
            candidates = list_candidates(conn, status=None, limit=limit)
        else:
            candidates = list_unclassified(conn, limit=limit)

        if not candidates:
            return summary

        if client is None:
            client = _build_anthropic_client()

        for batch in _chunks(candidates, batch_size):
            try:
                resp = _call_haiku(client, batch, model)
            except Exception as exc:
                logger.warning("classify batch failed: %s", exc)
                summary.errors += 1
                continue

            summary.batches += 1
            cost_usd = _estimate_cost(resp.input_tokens, resp.output_tokens)
            summary.cost_usd += cost_usd

            cost_record(
                conn,
                org_id=None,  # classification is not org-scoped
                call_type="anthropic_haiku_classify",
                cost_usd=cost_usd,
                model=model,
                input_tokens=resp.input_tokens,
                output_tokens=resp.output_tokens,
            )

            for c in batch:
                raw = resp.by_handle.get(c.handle_normalized) or resp.by_handle.get(
                    c.handle_normalized.lower()
                )
                if not isinstance(raw, dict):
                    logger.debug("no classification for %s", c.handle_normalized)
                    continue
                archetypes, sectors, status = _validate_classification(raw)
                update_classification(
                    conn,
                    candidate_id=c.candidate_id,  # type: ignore[arg-type]
                    archetype_tags=archetypes,
                    sector_tags=sectors,
                    status=status,
                )
                summary.classified += 1
                if status == "drop":
                    summary.dropped += 1

    return summary


def _chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


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
