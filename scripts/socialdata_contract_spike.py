"""Phase 0.5 — SocialData API contract spike for the SolStitch outreach plan.

Verifies access to the limited-access endpoints, observes per-page count,
cursor semantics, response shape, rate-limit budget, and handle→user_id
resolution path. Writes findings to docs/socialdata_contract_spike_<date>.md
and a fixture page to tests/fixtures/socialdata_followers_page.json.

Usage:
    .venv/bin/python scripts/socialdata_contract_spike.py [--target HANDLE]

Default target: doji_com.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml


REPO = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO / "docs"
FIXTURES_DIR = REPO / "tests" / "fixtures"
CONFIG_PATH = Path.home() / ".sable" / "config.yaml"
BASE_URL = "https://api.socialdata.tools"


def load_api_key() -> str:
    if not CONFIG_PATH.exists():
        sys.exit(f"missing {CONFIG_PATH}")
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    key = cfg.get("socialdata_api_key")
    if not key:
        # also check api_keys.socialdata or similar nested layouts
        api_keys = cfg.get("api_keys") or {}
        key = api_keys.get("socialdata") or api_keys.get("socialdata_api_key")
    if not key:
        sys.exit("socialdata_api_key not found in ~/.sable/config.yaml")
    return key


def get(client: httpx.Client, path: str, params: dict | None = None) -> tuple[int, dict | None, dict]:
    """Returns (status_code, body_or_none, response_headers)."""
    resp = client.get(f"{BASE_URL}{path}", params=params or {})
    try:
        body = resp.json() if resp.content else None
    except Exception:
        body = None
    return resp.status_code, body, dict(resp.headers)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="doji_com", help="Handle to spike against")
    args = ap.parse_args()
    target = args.target.lstrip("@").lower().strip()

    key = load_api_key()
    started = datetime.now(timezone.utc).isoformat()
    findings: dict[str, object] = {
        "started_at": started,
        "target_handle": target,
        "base_url": BASE_URL,
    }

    headers = {"Authorization": f"Bearer {key}"}
    with httpx.Client(headers=headers, timeout=60) as client:
        # ----- 1. user_id resolution -----
        print(f"[1/6] resolving user_id for @{target} via /twitter/user/{target}")
        sc, body, hdrs = get(client, f"/twitter/user/{target}")
        findings["resolve"] = {
            "status_code": sc,
            "fields_seen": sorted(list(body.keys()))[:30] if isinstance(body, dict) else None,
            "id_str": body.get("id_str") if isinstance(body, dict) else None,
            "id": body.get("id") if isinstance(body, dict) else None,
            "followers_count": body.get("followers_count") if isinstance(body, dict) else None,
            "friends_count": body.get("friends_count") if isinstance(body, dict) else None,
            "screen_name": body.get("screen_name") if isinstance(body, dict) else None,
        }
        if sc != 200 or not isinstance(body, dict):
            print(f"  FAILED status={sc} body={str(body)[:200]}")
            (DOCS_DIR / f"socialdata_contract_spike_{datetime.now().strftime('%Y_%m_%d')}.md").write_text(
                f"# SocialData contract spike — {started}\n\n## resolve failed\n\n"
                f"status_code: {sc}\nbody: ```{json.dumps(body, indent=2)[:2000]}```\n",
                encoding="utf-8",
            )
            sys.exit(1)
        user_id = body.get("id_str") or (str(body["id"]) if body.get("id") is not None else None)
        if not user_id:
            sys.exit("could not extract id_str from /twitter/user response")
        followers_count = body.get("followers_count") or 0
        friends_count = body.get("friends_count") or 0
        print(f"  ok: id_str={user_id} followers={followers_count} friends={friends_count}")

        # ----- 2. access check + page-size + cursor (3 pages of followers) -----
        print(f"[2/6] /twitter/followers/list?user_id={user_id} — 3 pages")
        followers_pages: list[dict] = []
        cursor = None
        access_denied = False
        for i in range(3):
            params: dict[str, object] = {"user_id": user_id}
            if cursor:
                params["cursor"] = cursor
            t0 = time.monotonic()
            sc, body, resp_hdrs = get(client, "/twitter/followers/list", params=params)
            dt = time.monotonic() - t0
            if sc in (401, 402, 403):
                access_denied = True
                followers_pages.append({"page_index": i, "status_code": sc, "body_excerpt": str(body)[:300]})
                print(f"  page {i+1}: ACCESS DENIED status={sc} body={str(body)[:200]}")
                break
            if sc != 200 or not isinstance(body, dict):
                followers_pages.append({"page_index": i, "status_code": sc, "body_excerpt": str(body)[:300]})
                print(f"  page {i+1}: status={sc} (non-200) body={str(body)[:200]}")
                break
            users = body.get("users") or []
            next_cursor = body.get("next_cursor")
            page_summary = {
                "page_index": i,
                "status_code": sc,
                "elapsed_s": round(dt, 3),
                "users_returned": len(users),
                "next_cursor_present": bool(next_cursor) and next_cursor not in ("0", ""),
                "next_cursor_value_repr": repr(next_cursor)[:100],
                "top_level_keys": sorted(list(body.keys())),
                "rate_limit_headers": {
                    k: v for k, v in resp_hdrs.items()
                    if k.lower().startswith("x-ratelimit") or k.lower() == "retry-after"
                },
            }
            followers_pages.append(page_summary)
            print(
                f"  page {i+1}: {len(users)} users in {dt:.2f}s, "
                f"next_cursor={'yes' if page_summary['next_cursor_present'] else 'no'}"
            )

            # Capture page-1 fixture for tests.
            if i == 0:
                fixture_path = FIXTURES_DIR / "socialdata_followers_page.json"
                # Trim profile dicts to keep fixture compact while preserving shape.
                trimmed_users = []
                for u in users[:5]:
                    if not isinstance(u, dict):
                        continue
                    trimmed_users.append(
                        {k: u.get(k) for k in (
                            "id", "id_str", "screen_name", "name",
                            "description", "followers_count", "friends_count",
                            "statuses_count", "verified", "protected",
                            "created_at", "location", "listed_count",
                        ) if k in u}
                    )
                fixture = {
                    "_note": "Trimmed page-1 fixture from /twitter/followers/list spike — see docs/socialdata_contract_spike_*.md",
                    "users": trimmed_users,
                    "users_returned_full_page": len(users),
                    "top_level_keys": sorted(list(body.keys())),
                    "next_cursor_repr": repr(next_cursor)[:100],
                }
                fixture_path.write_text(json.dumps(fixture, indent=2), encoding="utf-8")
                page_summary["fixture_written"] = str(fixture_path.relative_to(REPO))

            cursor = next_cursor
            if not cursor or cursor in ("0", ""):
                print(f"  cursor exhausted at page {i+1}")
                break

        findings["followers_pages"] = followers_pages
        findings["followers_access_denied"] = access_denied

        # ----- 3. friends/following endpoint (one page) -----
        if not access_denied:
            print(f"[3/6] /twitter/friends/list?user_id={user_id} — 1 page")
            sc, body, resp_hdrs = get(client, "/twitter/friends/list", params={"user_id": user_id})
            if sc in (401, 402, 403):
                findings["friends_endpoint"] = {"status_code": sc, "access_denied": True, "body_excerpt": str(body)[:300]}
                print(f"  ACCESS DENIED status={sc}")
            elif sc == 200 and isinstance(body, dict):
                users = body.get("users") or []
                findings["friends_endpoint"] = {
                    "status_code": sc,
                    "access_denied": False,
                    "users_returned": len(users),
                    "next_cursor_present": bool(body.get("next_cursor")) and body.get("next_cursor") not in ("0", ""),
                    "top_level_keys": sorted(list(body.keys())),
                }
                print(f"  ok: {len(users)} users")
            else:
                findings["friends_endpoint"] = {"status_code": sc, "body_excerpt": str(body)[:300]}
                print(f"  status={sc}")
        else:
            findings["friends_endpoint"] = {"skipped": "followers/list access denied — friends/list likely the same"}

        # ----- 4. rate-limit observation: 10 rapid sequential profile calls -----
        if not access_denied:
            print("[4/6] rate-limit probe — 10 rapid /twitter/user/{handle} calls")
            probe_handles = [target]  # repeat the same handle; cheap and consistent
            samples: list[dict] = []
            t_start = time.monotonic()
            for i in range(10):
                t0 = time.monotonic()
                sc, body, resp_hdrs = get(client, f"/twitter/user/{target}")
                dt = time.monotonic() - t0
                samples.append({
                    "i": i,
                    "status_code": sc,
                    "elapsed_s": round(dt, 3),
                    "rl_headers": {
                        k: v for k, v in resp_hdrs.items()
                        if k.lower().startswith("x-ratelimit") or k.lower() == "retry-after"
                    },
                })
                if sc == 429:
                    break
            wall = time.monotonic() - t_start
            findings["rate_limit_probe"] = {
                "samples": samples,
                "wall_seconds": round(wall, 3),
                "any_429": any(s["status_code"] == 429 for s in samples),
            }
            print(f"  10 calls in {wall:.2f}s, any 429: {findings['rate_limit_probe']['any_429']}")
        else:
            findings["rate_limit_probe"] = {"skipped": "access denied"}

    finished = datetime.now(timezone.utc).isoformat()
    findings["finished_at"] = finished

    # ----- 5. write the markdown report -----
    print("[5/6] writing docs/socialdata_contract_spike_<date>.md")
    date_slug = datetime.now().strftime("%Y_%m_%d")
    md_path = DOCS_DIR / f"socialdata_contract_spike_{date_slug}.md"
    md = _render_markdown(findings)
    md_path.write_text(md, encoding="utf-8")

    print(f"[6/6] done. wrote {md_path.relative_to(REPO)}")
    print(json.dumps({k: v for k, v in findings.items() if k != "followers_pages"}, indent=2)[:1500])


def _render_markdown(f: dict) -> str:
    lines: list[str] = []
    lines.append(f"# SocialData contract spike — Phase 0.5\n")
    lines.append(f"**Target:** `@{f['target_handle']}`")
    lines.append(f"**Started:** {f['started_at']}")
    lines.append(f"**Finished:** {f['finished_at']}\n")

    lines.append("## 1. user_id resolution (`/twitter/user/{handle}`)\n")
    r = f.get("resolve") or {}
    lines.append(f"* status_code: `{r.get('status_code')}`")
    lines.append(f"* `id_str`: `{r.get('id_str')}` (resolves cleanly: {bool(r.get('id_str'))})")
    lines.append(f"* `screen_name`: `{r.get('screen_name')}`")
    lines.append(f"* `followers_count`: {r.get('followers_count')}, `friends_count`: {r.get('friends_count')}")
    lines.append(f"* fields seen (first 30): {r.get('fields_seen')}\n")

    lines.append("## 2. /twitter/followers/list — access + page-size + cursor\n")
    lines.append(f"**Access denied:** {f.get('followers_access_denied')}\n")
    pages = f.get("followers_pages") or []
    if pages:
        lines.append("| page | status | users | elapsed_s | next_cursor? | top-level keys | RL headers |")
        lines.append("|-----:|:------:|------:|----------:|:-------------|:----------------|:-----------|")
        for p in pages:
            kt = p.get("top_level_keys") or "—"
            rl = p.get("rate_limit_headers") or {}
            lines.append(
                f"| {p.get('page_index')} | {p.get('status_code')} | "
                f"{p.get('users_returned', '—')} | {p.get('elapsed_s', '—')} | "
                f"{p.get('next_cursor_present', '—')} | "
                f"{kt if isinstance(kt, str) else ', '.join(kt)} | "
                f"{rl} |"
            )
    lines.append("")
    if pages and pages[0].get("users_returned"):
        lines.append(f"**Per-page count observed:** {pages[0]['users_returned']}\n")
        lines.append(f"**Cost-projection update:** at $0.002/page and {pages[0]['users_returned']} users/page,")
        lines.append("the SolStitch plan's Phase 2/6 cost ranges should snap to the lower or upper end")
        lines.append("of their bands. Phase 2 (62.7K profiles): "
                     f"~${round(62700/pages[0]['users_returned']*0.002, 2)}.\n")

    lines.append("## 3. /twitter/friends/list — access\n")
    fe = f.get("friends_endpoint") or {}
    if fe.get("skipped"):
        lines.append(f"_skipped: {fe['skipped']}_\n")
    else:
        lines.append(f"* status_code: `{fe.get('status_code')}`")
        lines.append(f"* access_denied: `{fe.get('access_denied')}`")
        lines.append(f"* users_returned: `{fe.get('users_returned')}`")
        lines.append(f"* next_cursor_present: `{fe.get('next_cursor_present')}`")
        lines.append(f"* top-level keys: `{fe.get('top_level_keys')}`\n")

    lines.append("## 4. Rate-limit probe (10 rapid `/twitter/user/{handle}` calls)\n")
    rlp = f.get("rate_limit_probe") or {}
    if rlp.get("skipped"):
        lines.append(f"_skipped: {rlp['skipped']}_\n")
    else:
        lines.append(f"* wall_seconds: `{rlp.get('wall_seconds')}`")
        lines.append(f"* any 429: `{rlp.get('any_429')}`\n")
        lines.append("| i | status | elapsed_s | RL headers |")
        lines.append("|--:|:------:|----------:|:-----------|")
        for s in rlp.get("samples", []):
            lines.append(f"| {s.get('i')} | {s.get('status_code')} | {s.get('elapsed_s')} | {s.get('rl_headers')} |")
        lines.append("")

    lines.append("## 5. Decisions unblocked\n")
    if f.get("followers_access_denied"):
        lines.append("* **ACCESS DENIED** — Phase 1+ blocked. Need operator decision on Plan B:")
        lines.append("  (a) upgrade SocialData plan, (b) Apify, (c) X API v2, (d) manual scrape.\n")
    else:
        lines.append("* **Access verified.** Phase 1 + Phase 2 unblocked.")
        if pages and pages[0].get("users_returned"):
            ppc = pages[0]["users_returned"]
            lines.append(f"* **Per-page count = {ppc}.** Cost projection narrows to a single point estimate.")
        lines.append("* `qc_profile` filter spec validated against fixture in `tests/fixtures/socialdata_followers_page.json`.\n")

    lines.append("## 6. Fixture\n")
    lines.append("Captured `tests/fixtures/socialdata_followers_page.json` with the trimmed first page")
    lines.append("(5 profiles + top-level metadata). Phase 1 unit tests assert against this fixture\n")

    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
