# SocialData contract spike — Phase 0.5

**Target:** `@doji_com`
**Started:** 2026-05-06T23:58:25.482313+00:00
**Finished:** 2026-05-06T23:58:40.099231+00:00

## 1. user_id resolution (`/twitter/user/{handle}`)

* status_code: `200`
* `id_str`: `1874926744890658816` (resolves cleanly: True)
* `screen_name`: `doji_com`
* `followers_count`: 5689, `friends_count`: 2
* fields seen (first 30): ['affiliation_label', 'can_dm', 'created_at', 'description', 'favourites_count', 'followers_count', 'friends_count', 'id', 'id_str', 'listed_count', 'location', 'name', 'profile_banner_url', 'profile_image_url_https', 'protected', 'screen_name', 'statuses_count', 'url', 'verification_info', 'verified']

## 2. /twitter/followers/list — access + page-size + cursor

**Access denied:** False

| page | status | users | elapsed_s | next_cursor? | top-level keys | RL headers |
|-----:|:------:|------:|----------:|:-------------|:----------------|:-----------|
| 0 | 200 | 49 | 1.673 | True | next_cursor, users | {'x-ratelimit-limit': '130', 'x-ratelimit-remaining': '128'} |
| 1 | 200 | 50 | 1.443 | True | next_cursor, users | {'x-ratelimit-limit': '130', 'x-ratelimit-remaining': '127'} |
| 2 | 200 | 50 | 1.412 | True | next_cursor, users | {'x-ratelimit-limit': '130', 'x-ratelimit-remaining': '126'} |

**Per-page count observed:** 49

**Cost-projection update:** at $0.002/page and 49 users/page,
the SolStitch plan's Phase 2/6 cost ranges should snap to the lower or upper end
of their bands. Phase 2 (62.7K profiles): ~$2.56.

## 3. /twitter/friends/list — access

* status_code: `200`
* access_denied: `False`
* users_returned: `2`
* next_cursor_present: `True`
* top-level keys: `['next_cursor', 'users']`

## 4. Rate-limit probe (10 rapid `/twitter/user/{handle}` calls)

* wall_seconds: `8.153`
* any 429: `False`

| i | status | elapsed_s | RL headers |
|--:|:------:|----------:|:-----------|
| 0 | 200 | 0.736 | {'x-ratelimit-limit': '130', 'x-ratelimit-remaining': '124'} |
| 1 | 200 | 1.052 | {'x-ratelimit-limit': '130', 'x-ratelimit-remaining': '123'} |
| 2 | 200 | 0.768 | {'x-ratelimit-limit': '130', 'x-ratelimit-remaining': '122'} |
| 3 | 200 | 0.804 | {'x-ratelimit-limit': '130', 'x-ratelimit-remaining': '121'} |
| 4 | 200 | 0.797 | {'x-ratelimit-limit': '130', 'x-ratelimit-remaining': '120'} |
| 5 | 200 | 0.787 | {'x-ratelimit-limit': '130', 'x-ratelimit-remaining': '119'} |
| 6 | 200 | 0.796 | {'x-ratelimit-limit': '130', 'x-ratelimit-remaining': '118'} |
| 7 | 200 | 0.785 | {'x-ratelimit-limit': '130', 'x-ratelimit-remaining': '117'} |
| 8 | 200 | 0.823 | {'x-ratelimit-limit': '130', 'x-ratelimit-remaining': '116'} |
| 9 | 200 | 0.805 | {'x-ratelimit-limit': '130', 'x-ratelimit-remaining': '115'} |

## 5. Decisions unblocked

* **Access verified.** Phase 1 + Phase 2 unblocked.
* **Per-page count = 49.** Cost projection narrows to a single point estimate.
* `qc_profile` filter spec validated against fixture in `tests/fixtures/socialdata_followers_page.json`.

## 6. Fixture

Captured `tests/fixtures/socialdata_followers_page.json` with the trimmed first page
(5 profiles + top-level metadata). Phase 1 unit tests assert against this fixture

