"""SocialData live-data fetcher for KO-3 v2 enrichment.

Replaces the "Grok will use live X search" assumption (which doesn't
actually work — see commit history around 2026-05-10 when Grok-4-latest
admitted it has no real-time X access). Now Grok gets verbatim tweets +
verified profile data fetched from SocialData; its job becomes
**interpretation**, not search.

In-repo httpx implementation (no Slopper dep), same pattern as
``handle_verifier.py``. Sidecar already has ``SOCIALDATA_API_KEY`` in env.

Cost: per enrichment, 1 profile fetch (~$0.0002) + 1 tweet-page fetch
(~$0.0002 × N results) ≈ $0.004 for 20 tweets. Negligible vs the
previous (fictional) Grok live-search cost.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx


logger = logging.getLogger(__name__)

SOCIALDATA_BASE_URL = "https://api.socialdata.tools"

DEFAULT_TWEET_COUNT = 20
DEFAULT_TIMEOUT = 30.0


class LiveDataUnavailableError(RuntimeError):
    """Raised when SocialData cannot be reached or returns a fatal error.

    The sidecar maps this to HTTP 503 — without real tweet content the
    enrichment falls back to fabricated output, which is exactly what
    KO-3 v2 was designed to avoid.
    """


class LiveDataHandleNotFoundError(LiveDataUnavailableError):
    """The candidate handle does not resolve on X (404 / suspended / deleted)."""


class LiveDataBalanceExhaustedError(LiveDataUnavailableError):
    """SocialData balance is depleted (HTTP 402)."""


@dataclass(slots=True, frozen=True)
class LiveTweet:
    """One verbatim tweet from the candidate's timeline.

    All fields are taken straight from SocialData's response. ``text``
    prefers ``full_text`` over ``text`` when present (full_text doesn't
    truncate replies/quotes the way ``text`` does on the legacy API
    shape SocialData mirrors).
    """

    timestamp: str | None
    """ISO timestamp if available (from ``created_at``)."""
    type: str
    """"post" | "reply" | "retweet"."""
    in_reply_to: str | None
    """Bare handle if reply, else None."""
    retweeted_from: str | None
    """Bare handle if retweet, else None."""
    text: str
    """Verbatim tweet content, ≤500 chars (trimmed for prompt safety)."""


@dataclass(slots=True, frozen=True)
class LiveProfile:
    """Canonical X profile snapshot."""

    handle: str
    user_id: str | None
    """Numeric X user ID (``id_str``). Required to call the /tweets endpoint —
    SocialData's `/twitter/user/<screen_name>/tweets` 404s, only
    `/twitter/user/<numeric_id>/tweets` works. Discovered 2026-05-10 when
    the v2.5 SocialData swap landed."""
    real_name: str | None
    bio: str
    location: str | None
    followers_count: int | None
    following_count: int | None
    listed_count: int | None
    statuses_count: int | None
    verified: bool
    protected: bool = False
    """True if the X account is locked. SocialData returns the profile but
    refuses the timeline fetch in that case — treat as 'no tweets'."""


@dataclass(slots=True, frozen=True)
class LiveSignal:
    """Combined profile + tweet pull for one handle."""

    profile: LiveProfile
    tweets: list[LiveTweet]
    fetched_at_utc: str


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


def _normalize_handle(handle: str) -> str:
    return handle.lstrip("@").strip().lower()


def _api_key() -> str:
    key = os.environ.get("SOCIALDATA_API_KEY")
    if not key:
        raise LiveDataUnavailableError(
            "SOCIALDATA_API_KEY is not set. KO-3 v2 enrichment requires "
            "SocialData for live profile + tweet content; without it the "
            "feature falls back to Grok confabulation. Add the key to the "
            "sidecar env."
        )
    return key


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_api_key()}",
        "Accept": "application/json",
    }


def _check_response(resp: httpx.Response, *, path: str) -> dict:
    """Validate SocialData response; raise typed errors for known failures."""
    if resp.status_code == 402:
        raise LiveDataBalanceExhaustedError(
            f"SocialData balance exhausted (HTTP 402) on {path}. "
            "Top up account — no retry will help."
        )
    if resp.status_code in (404, 410):
        raise LiveDataHandleNotFoundError(
            f"SocialData {resp.status_code} on {path} — handle does not "
            "resolve on X (deleted, suspended, or never existed)."
        )
    if resp.status_code != 200:
        raise LiveDataUnavailableError(
            f"SocialData HTTP {resp.status_code} on {path}: "
            f"{resp.text[:200]}"
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise LiveDataUnavailableError(
            f"SocialData JSON parse error on {path}: {exc}"
        ) from exc
    # SocialData returns 200 with {"status": "error", ...} for some not-found
    # cases — handle here so callers don't have to repeat the check.
    if isinstance(data, dict) and data.get("status") == "error":
        message = data.get("message", "?")
        raise LiveDataHandleNotFoundError(
            f"SocialData says @{path.split('/')[-1]} not found: {message}"
        )
    return data


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def fetch_profile(
    handle: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> LiveProfile:
    """GET /twitter/user/{handle} → :class:`LiveProfile`.

    Costs ~$0.0002 per call. Raises:
        LiveDataHandleNotFoundError: handle doesn't resolve.
        LiveDataBalanceExhaustedError: SocialData credits depleted.
        LiveDataUnavailableError: any other failure (network, 5xx, auth).
    """
    h = _normalize_handle(handle)
    path = f"/twitter/user/{h}"
    url = f"{SOCIALDATA_BASE_URL}{path}"

    if client is not None:
        resp = client.get(url, headers=_headers(), timeout=timeout)
    else:
        with httpx.Client(timeout=timeout) as c:
            resp = c.get(url, headers=_headers())

    data = _check_response(resp, path=path)
    return _parse_profile(data, h)


def fetch_recent_tweets(
    user_id: str,
    *,
    count: int = DEFAULT_TWEET_COUNT,
    client: httpx.Client | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[LiveTweet]:
    """GET /twitter/user/{user_id}/tweets — fetches recent tweets by NUMERIC ID.

    Critical: ``user_id`` is the numeric X user ID (``id_str`` from the
    profile endpoint), NOT the screen name. SocialData's tweets endpoint
    404s on screen names. Callers should chain
    :func:`fetch_profile` first to resolve the ID, or use
    :func:`fetch_live_signal` which handles both.

    Returns at most ``count`` recent tweets (posts + replies + retweets,
    not filtered). May return fewer if the account is quiet. Cost:
    $0.0002 per result returned.

    Raises:
        LiveDataHandleNotFoundError: user_id doesn't resolve.
        LiveDataBalanceExhaustedError: 402.
        LiveDataUnavailableError: any other failure.
    """
    if not user_id:
        return []
    path = f"/twitter/user/{user_id}/tweets"
    url = f"{SOCIALDATA_BASE_URL}{path}"
    params = {"type": "tweets", "limit": count}

    if client is not None:
        resp = client.get(url, headers=_headers(), params=params, timeout=timeout)
    else:
        with httpx.Client(timeout=timeout) as c:
            resp = c.get(url, headers=_headers(), params=params)

    data = _check_response(resp, path=path)
    raw_tweets = data.get("tweets") if isinstance(data, dict) else None
    if raw_tweets is None and isinstance(data, dict):
        raw_tweets = data.get("data", [])
    if not isinstance(raw_tweets, list):
        return []
    return [_parse_tweet(t) for t in raw_tweets[:count]]


def fetch_live_signal(
    handle: str,
    *,
    tweet_count: int = DEFAULT_TWEET_COUNT,
    client: httpx.Client | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> LiveSignal:
    """Combined fetch: profile + recent tweets in one call.

    Uses a shared httpx.Client so the TLS handshake amortizes across both
    requests. Returns a :class:`LiveSignal` with the canonical
    ``fetched_at_utc`` timestamp. Total cost ~$0.004 for 20 tweets.
    """
    from datetime import datetime, timezone

    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=timeout)
    try:
        profile = fetch_profile(handle, client=client, timeout=timeout)
        # SocialData's /tweets endpoint requires the numeric user_id, not
        # the screen name. We chain via the profile we just fetched. If
        # the account is locked (protected=true), skip the tweets call —
        # we'd 401 or get an empty payload anyway.
        if profile.protected or not profile.user_id:
            tweets: list[LiveTweet] = []
        else:
            try:
                tweets = fetch_recent_tweets(
                    profile.user_id,
                    count=tweet_count,
                    client=client,
                    timeout=timeout,
                )
            except LiveDataHandleNotFoundError:
                # /tweets 404 for a valid user typically means "no public
                # tweets returned" rather than "user doesn't exist" —
                # surface as empty list, profile still carries signal.
                tweets = []
    finally:
        if owns_client and client is not None:
            client.close()

    fetched_at = (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    return LiveSignal(profile=profile, tweets=tweets, fetched_at_utc=fetched_at)


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _parse_profile(data: dict, handle: str) -> LiveProfile:
    """Map SocialData's /twitter/user response to :class:`LiveProfile`.

    SocialData returns the legacy-API field names: ``screen_name``,
    ``name``, ``description``, ``followers_count``, ``friends_count``
    (= following), ``listed_count``, ``statuses_count``, ``verified``,
    ``location``, ``protected``, ``id_str``. Plus newer fields like
    ``ext_is_blue_verified`` — ignored here.
    """
    return LiveProfile(
        handle=handle,
        user_id=_str_or_none(data.get("id_str")) or _str_or_none(data.get("id")),
        real_name=_str_or_none(data.get("name")),
        bio=(data.get("description") or "")[:600],
        location=_str_or_none(data.get("location")),
        followers_count=_int_or_none(data.get("followers_count")),
        following_count=_int_or_none(data.get("friends_count")),
        listed_count=_int_or_none(data.get("listed_count")),
        statuses_count=_int_or_none(data.get("statuses_count")),
        verified=bool(data.get("verified") or data.get("ext_is_blue_verified")),
        protected=bool(data.get("protected", False)),
    )


def _parse_tweet(data: dict) -> LiveTweet:
    """Map one SocialData tweet object to :class:`LiveTweet`.

    Prefers ``full_text`` over ``text`` to capture untruncated replies
    and quote-tweets. Reply/retweet detection uses the standard fields
    (``in_reply_to_screen_name``, ``retweeted_status``).
    """
    text = (data.get("full_text") or data.get("text") or "")[:500]
    timestamp = _str_or_none(data.get("created_at")) or _str_or_none(
        data.get("tweet_created_at")
    )

    if isinstance(data.get("retweeted_status"), dict):
        rt = data["retweeted_status"]
        original = (
            rt.get("user", {}).get("screen_name")
            or rt.get("screen_name")
            or rt.get("user_screen_name")
        )
        return LiveTweet(
            timestamp=timestamp,
            type="retweet",
            in_reply_to=None,
            retweeted_from=_str_or_none(original),
            text=text,
        )

    in_reply = _str_or_none(data.get("in_reply_to_screen_name"))
    if in_reply:
        return LiveTweet(
            timestamp=timestamp,
            type="reply",
            in_reply_to=in_reply,
            retweeted_from=None,
            text=text,
        )

    return LiveTweet(
        timestamp=timestamp,
        type="post",
        in_reply_to=None,
        retweeted_from=None,
        text=text,
    )


def _str_or_none(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _int_or_none(v) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


__all__ = [
    "DEFAULT_TWEET_COUNT",
    "LiveDataBalanceExhaustedError",
    "LiveDataHandleNotFoundError",
    "LiveDataUnavailableError",
    "LiveProfile",
    "LiveSignal",
    "LiveTweet",
    "fetch_live_signal",
    "fetch_profile",
    "fetch_recent_tweets",
]
