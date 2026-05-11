"""Tests for sable_kol.socialdata_live — SocialData fetch + parse.

Mocks SocialData HTTP via httpx.MockTransport so no real network or
billing happens. Covers: happy-path profile + tweet fetch, the three
typed-error paths (404 / 402 / generic 5xx), parse-shape edge cases
(reply vs retweet vs post), and the missing-API-key fallback.
"""
from __future__ import annotations

import httpx
import pytest

from sable_kol import socialdata_live as sd


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _profile_response(handle: str = "alice", **overrides) -> dict:
    base = {
        "id": 12345,
        "id_str": "12345",
        "screen_name": handle,
        "name": "Alice Doe",
        "description": "convex optimization, occasional crypto curiosity",
        "location": "NYC",
        "followers_count": 12000,
        "friends_count": 900,
        "listed_count": 80,
        "statuses_count": 4500,
        "verified": False,
        "protected": False,
    }
    base.update(overrides)
    return base


def _tweets_response(**overrides) -> dict:
    base = {
        "tweets": [
            {
                "id": 1,
                "full_text": "the alphaevolve paper finally clicked for me",
                "created_at": "2026-05-08T14:00:00Z",
            },
            {
                "id": 2,
                "full_text": "agreed — the typewriter aesthetic is the point",
                "created_at": "2026-05-07T20:00:00Z",
                "in_reply_to_screen_name": "doreen",
            },
            {
                "id": 3,
                "full_text": "FWB is the model for tasteful crypto-adjacent communities",
                "created_at": "2026-05-06T10:00:00Z",
                "retweeted_status": {
                    "user": {"screen_name": "punk6529"},
                },
            },
        ]
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


def test_missing_api_key_raises_unavailable(monkeypatch):
    monkeypatch.delenv("SOCIALDATA_API_KEY", raising=False)
    with pytest.raises(sd.LiveDataUnavailableError, match="SOCIALDATA_API_KEY"):
        sd.fetch_profile("alice")


# ---------------------------------------------------------------------------
# Profile fetch
# ---------------------------------------------------------------------------


def test_fetch_profile_happy(monkeypatch):
    monkeypatch.setenv("SOCIALDATA_API_KEY", "sd-test")
    client = _mock_client(lambda req: httpx.Response(200, json=_profile_response()))
    profile = sd.fetch_profile("@Alice", client=client)
    assert profile.handle == "alice"  # normalized lowercase, @-stripped
    assert profile.real_name == "Alice Doe"
    assert profile.bio.startswith("convex optimization")
    assert profile.location == "NYC"
    assert profile.followers_count == 12_000
    assert profile.following_count == 900  # friends_count → following_count
    assert profile.verified is False


def test_fetch_profile_404_raises_handle_not_found(monkeypatch):
    monkeypatch.setenv("SOCIALDATA_API_KEY", "sd-test")
    client = _mock_client(lambda req: httpx.Response(404, text="not found"))
    with pytest.raises(sd.LiveDataHandleNotFoundError):
        sd.fetch_profile("ghost", client=client)


def test_fetch_profile_402_raises_balance_exhausted(monkeypatch):
    monkeypatch.setenv("SOCIALDATA_API_KEY", "sd-test")
    client = _mock_client(lambda req: httpx.Response(402, text="balance"))
    with pytest.raises(sd.LiveDataBalanceExhaustedError):
        sd.fetch_profile("alice", client=client)


def test_fetch_profile_500_raises_unavailable(monkeypatch):
    monkeypatch.setenv("SOCIALDATA_API_KEY", "sd-test")
    client = _mock_client(lambda req: httpx.Response(500, text="oops"))
    with pytest.raises(sd.LiveDataUnavailableError):
        sd.fetch_profile("alice", client=client)


def test_fetch_profile_200_with_error_status_raises_handle_not_found(monkeypatch):
    """SocialData returns 200 with {'status':'error'} for some not-found cases."""
    monkeypatch.setenv("SOCIALDATA_API_KEY", "sd-test")
    client = _mock_client(
        lambda req: httpx.Response(200, json={"status": "error", "message": "User not found"})
    )
    with pytest.raises(sd.LiveDataHandleNotFoundError):
        sd.fetch_profile("alice", client=client)


# ---------------------------------------------------------------------------
# Tweet fetch + parse
# ---------------------------------------------------------------------------


def test_fetch_recent_tweets_happy_and_parses_all_three_types(monkeypatch):
    monkeypatch.setenv("SOCIALDATA_API_KEY", "sd-test")
    client = _mock_client(lambda req: httpx.Response(200, json=_tweets_response()))
    # /tweets endpoint takes numeric user_id, not screen name (SocialData quirk).
    tweets = sd.fetch_recent_tweets("12345", count=5, client=client)
    assert len(tweets) == 3
    # Post
    assert tweets[0].type == "post"
    assert tweets[0].in_reply_to is None
    assert tweets[0].retweeted_from is None
    assert tweets[0].text.startswith("the alphaevolve")
    # Reply
    assert tweets[1].type == "reply"
    assert tweets[1].in_reply_to == "doreen"
    assert tweets[1].retweeted_from is None
    # Retweet
    assert tweets[2].type == "retweet"
    assert tweets[2].retweeted_from == "punk6529"
    assert tweets[2].in_reply_to is None


def test_fetch_recent_tweets_caps_text_at_500c(monkeypatch):
    monkeypatch.setenv("SOCIALDATA_API_KEY", "sd-test")
    monster_tweet = {"id": 1, "full_text": "x" * 1000}
    client = _mock_client(
        lambda req: httpx.Response(200, json={"tweets": [monster_tweet]})
    )
    tweets = sd.fetch_recent_tweets("12345", count=1, client=client)
    assert len(tweets[0].text) == 500


def test_fetch_recent_tweets_prefers_full_text_over_text(monkeypatch):
    """full_text is the legacy-API field that doesn't truncate replies."""
    monkeypatch.setenv("SOCIALDATA_API_KEY", "sd-test")
    payload = {
        "tweets": [
            {"id": 1, "text": "truncated…", "full_text": "complete full text here"},
        ]
    }
    client = _mock_client(lambda req: httpx.Response(200, json=payload))
    tweets = sd.fetch_recent_tweets("12345", count=1, client=client)
    assert tweets[0].text == "complete full text here"


def test_fetch_recent_tweets_empty_payload_returns_empty_list(monkeypatch):
    """SocialData returns {} or {tweets: []} when the account is quiet —
    not an error, just no recent activity. Caller handles gracefully."""
    monkeypatch.setenv("SOCIALDATA_API_KEY", "sd-test")
    client = _mock_client(lambda req: httpx.Response(200, json={"tweets": []}))
    tweets = sd.fetch_recent_tweets("12345", count=5, client=client)
    assert tweets == []


def test_fetch_recent_tweets_respects_count_cap(monkeypatch):
    monkeypatch.setenv("SOCIALDATA_API_KEY", "sd-test")
    payload = {"tweets": [{"id": i, "full_text": f"tweet {i}"} for i in range(50)]}
    client = _mock_client(lambda req: httpx.Response(200, json=payload))
    tweets = sd.fetch_recent_tweets("12345", count=10, client=client)
    assert len(tweets) == 10


def test_fetch_recent_tweets_empty_user_id_returns_empty(monkeypatch):
    """Guard against caller passing empty user_id (no profile fetched)."""
    monkeypatch.setenv("SOCIALDATA_API_KEY", "sd-test")
    assert sd.fetch_recent_tweets("", count=5) == []


def test_fetch_live_signal_protected_account_skips_tweet_call(monkeypatch):
    """Locked timeline: profile fetched, tweet call skipped."""
    monkeypatch.setenv("SOCIALDATA_API_KEY", "sd-test")

    call_count = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json=_profile_response(protected=True))

    client = _mock_client(handler)
    live = sd.fetch_live_signal("alice", client=client)
    assert live.profile.protected is True
    assert live.tweets == []
    # Only the profile endpoint was hit (not the tweets endpoint).
    assert call_count["n"] == 1


def test_fetch_live_signal_tweets_404_returns_empty_not_error(monkeypatch):
    """A 404 on /tweets when the profile resolves means 'no public tweets
    for this account right now' — not a fatal error."""
    monkeypatch.setenv("SOCIALDATA_API_KEY", "sd-test")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/tweets"):
            return httpx.Response(404)
        return httpx.Response(200, json=_profile_response())

    client = _mock_client(handler)
    live = sd.fetch_live_signal("alice", client=client)
    # Profile + handle resolved fine; tweets just unavailable for this account.
    assert live.profile.handle == "alice"
    assert live.profile.user_id == "12345"
    assert live.tweets == []


# ---------------------------------------------------------------------------
# Combined fetch_live_signal
# ---------------------------------------------------------------------------


def test_fetch_live_signal_combines_profile_and_tweets(monkeypatch):
    monkeypatch.setenv("SOCIALDATA_API_KEY", "sd-test")

    requested_paths = []

    def handler(req: httpx.Request) -> httpx.Response:
        requested_paths.append(req.url.path)
        if req.url.path.endswith("/tweets"):
            return httpx.Response(200, json=_tweets_response())
        return httpx.Response(200, json=_profile_response())

    client = _mock_client(handler)
    live = sd.fetch_live_signal("alice", tweet_count=5, client=client)
    assert live.profile.handle == "alice"
    assert live.profile.user_id == "12345"
    assert live.profile.real_name == "Alice Doe"
    assert len(live.tweets) == 3
    assert live.fetched_at_utc.endswith("Z")
    assert "T" in live.fetched_at_utc  # ISO
    # Tweets endpoint was hit with the numeric user_id, NOT the screen name.
    tweet_paths = [p for p in requested_paths if p.endswith("/tweets")]
    assert tweet_paths == ["/twitter/user/12345/tweets"]


def test_fetch_live_signal_propagates_handle_not_found(monkeypatch):
    """If the profile fetch 404s, fetch_live_signal raises without
    wasting the tweet-page call."""
    monkeypatch.setenv("SOCIALDATA_API_KEY", "sd-test")

    call_count = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(404)

    client = _mock_client(handler)
    with pytest.raises(sd.LiveDataHandleNotFoundError):
        sd.fetch_live_signal("ghost", client=client)
    # Profile fetch hit, tweet fetch never called.
    assert call_count["n"] == 1
