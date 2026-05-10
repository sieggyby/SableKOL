"""SocialData handle verifier — code-side belt+suspenders against Grok's
unreliable ``handle_verified=true`` claim.

Per ``feedback_grok_handle_verification.md``, even with explicit HANDLE
VERIFICATION language in ``_build_comparable_prompt`` and the
``handle_verified`` schema field, Grok hallucinates X handles that don't
resolve on live X (~20–50 % rate observed across two TIG preflight runs).
This module hits SocialData's ``/twitter/user/<handle>`` endpoint to
ground-truth each suggestion before it reaches the bank or the wizard UI.

Cost: ~$0.001 per handle (SocialData base rate × 1 page) × 3× empirical
multiplier from ``feedback_cost_estimate_framing`` ≈ ~$0.003 per handle.
For a typical 8–10-handle comparable response, ~$0.03 per preflight call.

Graceful degradation: if ``SOCIALDATA_API_KEY`` is unset (e.g. local dev
without the key, or a sidecar env that hasn't been rotated to include
SocialData yet), :func:`verify_handle` logs a warning and returns the
last-resort answer the caller asked for via ``default``. Production
sidecar should always have the key set; the fallback exists so that
adding the validator doesn't break deploys mid-rollout.
"""
from __future__ import annotations

import logging
import os

import httpx


logger = logging.getLogger(__name__)

SOCIALDATA_BASE_URL = "https://api.socialdata.tools"


def _normalize_handle(handle: str) -> str:
    return handle.lstrip("@").strip().lower()


def verify_handle(
    handle: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 5.0,
    default: bool = True,
) -> bool:
    """Return True iff the X handle exists and is not suspended on live X.

    Args:
        handle: bare X handle (with or without leading @).
        client: optional shared httpx.Client to amortize TLS handshake across
            many lookups in a batch.
        timeout: per-request timeout in seconds.
        default: value returned when the SOCIALDATA_API_KEY env var is not set
            or a transient network error prevents verification. Defaults to
            ``True`` (don't drop on infra failure — fail open) so that a
            preflight sidecar without the key configured behaves identically
            to the pre-validator behavior. Pass ``default=False`` from
            stricter callers (e.g. the operator-confirm path) that want to
            fail closed.
    """
    api_key = os.environ.get("SOCIALDATA_API_KEY")
    if not api_key:
        logger.warning(
            "SOCIALDATA_API_KEY missing — handle verification skipped for @%s "
            "(returning default=%s). Add the key to the sidecar env to enable.",
            _normalize_handle(handle),
            default,
        )
        return default

    h = _normalize_handle(handle)
    url = f"{SOCIALDATA_BASE_URL}/twitter/user/{h}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }

    try:
        if client is not None:
            resp = client.get(url, headers=headers, timeout=timeout)
        else:
            with httpx.Client(timeout=timeout) as c:
                resp = c.get(url, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning(
            "SocialData verify failed (network error) for @%s: %s — returning default=%s",
            h, exc, default,
        )
        return default

    # 404 / 410: definitive "this handle does not resolve on X right now".
    # SocialData returns these for handles that don't exist (most common
    # Grok hallucination shape). Treat as drop, not infra failure.
    if resp.status_code in (404, 410):
        logger.info(
            "SocialData says @%s returns HTTP %s — handle does not exist, dropping",
            h, resp.status_code,
        )
        return False

    if resp.status_code != 200:
        # 401/403 (auth), 429 (rate-limit), 5xx (server) — don't drop the
        # handle on infra issues; let the operator re-validate manually.
        logger.warning(
            "SocialData verify HTTP %s for @%s — infra error, returning default=%s",
            resp.status_code, h, default,
        )
        return default

    try:
        data = resp.json()
    except ValueError as exc:
        logger.warning(
            "SocialData verify JSON parse error for @%s: %s — returning default=%s",
            h, exc, default,
        )
        return default

    # SocialData also returns ``{"status": "error", "message": "User not found" |
    # "User is suspended"}`` with HTTP 200 in some cases. Same drop semantics.
    if data.get("status") == "error":
        message = data.get("message", "?")
        logger.info(
            "SocialData says @%s does not exist on X (%s) — dropping",
            h, message,
        )
        return False

    return True


def verify_handles(
    handles: list[str],
    *,
    timeout: float = 5.0,
    default: bool = True,
) -> dict[str, bool]:
    """Verify many handles in one shared HTTP client. Returns a dict mapping
    each input handle (normalized — lower-cased, leading @ stripped) to its
    verification verdict. Order preserved relative to input.
    """
    out: dict[str, bool] = {}
    if not handles:
        return out
    with httpx.Client(timeout=timeout) as client:
        for h in handles:
            normalized = _normalize_handle(h)
            out[normalized] = verify_handle(
                h, client=client, timeout=timeout, default=default,
            )
    return out
