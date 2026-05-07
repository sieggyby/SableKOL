"""Cost-event logging for SableKOL.

Every paid call (Anthropic + SocialData) writes a row to ``cost_events`` via
SablePlatform's ``log_cost``. This is the single accounting surface — Slopper's
``socialdata_get_async`` does NOT log cost (it's an error/rate-limit wrapper),
so SableKOL is responsible for booking its own usage.

Path-(ii) external handles use the sentinel ``org_id='_external'``. The first
cost event for ``_external`` lazily creates the sentinel org row so the
``cost_events.org_id → orgs.org_id`` FK doesn't fire.
"""
from __future__ import annotations

from typing import Any

# call_type prefix for everything SableKOL writes. Useful for cost rollups.
CALL_TYPE_PREFIX = "sablekol."

EXTERNAL_ORG_ID = "_external"


def _ensure_external_org(conn: Any) -> None:
    """Create the ``_external`` sentinel org if it doesn't exist."""
    row = conn.execute(
        "SELECT 1 AS x FROM orgs WHERE org_id = :id",
        {"id": EXTERNAL_ORG_ID},
    ).fetchone()
    if row:
        return
    conn.execute(
        "INSERT INTO orgs (org_id, display_name, status, config_json) "
        "VALUES (:id, :name, 'active', '{}')",
        {"id": EXTERNAL_ORG_ID, "name": "External (SableKOL path-ii sentinel)"},
    )
    conn.commit()


def record(
    conn: Any,
    *,
    org_id: str | None,
    call_type: str,
    cost_usd: float,
    model: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    call_status: str = "success",
) -> None:
    """Log a paid call to ``cost_events``.

    ``org_id`` may be None — in that case we use the ``_external`` sentinel.
    ``call_type`` should be one of:

    * ``"anthropic_haiku_classify"`` — classification prompt
    * ``"anthropic_haiku_rationale"`` — match-rationale prompt
    * ``"socialdata_user_profile"`` — single ``GET /twitter/user/{handle}``
    * ``"socialdata_user_profile_resolve"`` — handle→user_id resolution
      (one paid profile fetch when bank.twitter_id is unknown)
    * ``"socialdata_followers_page"`` — one page of
      ``GET /twitter/followers/list`` (SolStitch follow-graph plan)
    * ``"socialdata_following_page"`` — one page of
      ``GET /twitter/friends/list`` (SolStitch follow-graph plan)

    The CALL_TYPE_PREFIX is prepended automatically.
    """
    effective_org = org_id or EXTERNAL_ORG_ID
    if effective_org == EXTERNAL_ORG_ID:
        _ensure_external_org(conn)
    full_call_type = f"{CALL_TYPE_PREFIX}{call_type}"
    # Reuse SablePlatform's logger for consistency. It accepts a CompatConnection.
    from sable_platform.db.cost import log_cost
    log_cost(
        conn,
        org_id=effective_org,
        call_type=full_call_type,
        cost_usd=cost_usd,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        call_status=call_status,
    )
