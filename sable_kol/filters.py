"""Shared filter predicates for SableKOL deliverables.

These are operator-judgment filters applied at deliverable-generation time.
They are NOT applied at the bank level (the bank stays inclusive — these
people may be relevant for other projects). Filtering happens when we
write an outreach plan, network graph, or any client-facing artifact.

Three filters, applied in order:

  candidates → drop is_organization → drop is_celebrity → drop is_unreachable
              → keep person_allowlist (hand-curated overrides)

`is_organization` — accounts that are brands / projects / protocols /
platforms. They don't read DMs; outreach should target the humans behind.

`is_celebrity` — accounts with massive broadcast reach who do not engage
with "random crypto projects". Includes mainstream celebrities (Paris
Hilton, Diplo) and crypto-OG broadcast accounts (Elon, CZ, Vitalik,
Saylor, ApomP). These are reachable in theory, ineffective in practice.

A `PERSON_ALLOWLIST` overrides false positives in either filter.

Per-client overrides will live in `~/.sable/clients/<id>.yaml` once Phase 2
of the generalization plan ships (see
`docs/sablekol_generalization_plan.md`). For now, the lists below are
global.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# Organization filter
# ---------------------------------------------------------------------------

ORG_DENYLIST = {
    # Marketplaces / platforms
    "opensea", "rarible", "blur_io", "looksrare", "magic_eden", "magiceden",
    "rugradio", "nft_nyc", "coindesk", "highsnobiety_official",
    # Brands / projects / audience targets
    "thefabricant", "9dccxyz", "doji_com",
    "rtfkt", "yugalabs", "boredapeyc", "cryptopunks", "decentraland",
    "worldofwomenxyz", "othersidemeta",
    # Protocols / chains / infra
    "ethereum", "0xpolygon", "buildonbase", "ledger", "metamask",
    "infura_io", "alchemy", "uniswap", "binance", "coinbase",
    "ipfsofficial", "aave", "compoundfinance", "makerdao", "starknet",
    # Cohort-extracted brand accounts surfaced in kingmaker output
    "artblocks_io", "showstudio", "monaverse", "dressxcom", "pixelvault_",
    "infiniteobjects", "krwn_studio", "mntge_io", "auroboros_ltd",
    "mutani_io", "dapewives", "axiesisters", "abstractorsnft",
    "themetajuice", "another1_io", "hellometaversal", "arianeeproject",
    "aeoniumsky", "slickcitynft", "thedigitaldogs", "sailormarsnft",
    "fashionweekonline", "iyk_app", "moodyowlnft", "artontezos_",
    "trilitech", "spatial_io", "rplanetnft", "unionavatars",
    "flowergirlsnft", "toygersofficial", "cheb_inc", "remx_xyz",
    "cuedotfun", "vcaresidency", "verticalcrypto", "prooofofpeople",
    "betterasaweb", "nftinsider_io", "abdroid_xyz", "aventurinelabs",
    "tributelabsxyz", "adinonline", "flamingobluexyz", "net__society",
    "nftmorning", "nftfactoryparis", "bemyappofficial", "bemyapp",
    "lvmh", "obvious_official", "obv_ious",
}

ORG_HANDLE_SUFFIXES = (
    "_io", "_xyz", "_app", "_hq", "_labs", "_studio", "_protocol",
    "_network", "_foundation", "_official", "_dao", "_inc", "_ltd",
    "_eth", "_finance",
)
ORG_HANDLE_SUBSTRINGS = (
    "official", "labs", "studio", "protocol", "foundation",
    "dao", "network", "marketplace", "exchange",
)

PERSON_ALLOWLIST = {
    "betty_nft",        # Bored Ape co-creator's wife, real person
    "punk6529",         # anon person not org despite handle
    "loomdart",         # operator-pinned person
    "toomuchlag",       # operator-pinned person
}


def is_organization(handle: str, archetypes: list[str], bio: str | None) -> bool:
    """True if the handle looks like an org/brand/platform.

    See module docstring for ordering rationale.
    """
    h = (handle or "").lower()
    if not h:
        return False
    if h in PERSON_ALLOWLIST:
        return False
    if h in ORG_DENYLIST:
        return True
    for s in ORG_HANDLE_SUFFIXES:
        if h.endswith(s):
            return True
    for s in ORG_HANDLE_SUBSTRINGS:
        if s in h:
            return True
    # Person archetypes the bank emits. "artist" and "creator" appear in
    # legacy bank data even though they're not in the current
    # VALID_ARCHETYPES set in classify.py — missing them flagged real
    # artists (e.g. @ogdfarmer ["ecosystem","artist"]) as orgs.
    person_archetypes = {"thought_leader", "connector", "dev", "anon",
                         "founder", "researcher", "trader",
                         "artist", "creator"}
    if archetypes:
        as_set = set(archetypes)
        if "ecosystem" in as_set and not (as_set & person_archetypes):
            return True
    if bio:
        b = bio.strip().lower()[:80]
        for prefix in ("we are ", "we're ", "we build", "our team",
                       "the official ", "powered by ", "the team behind"):
            if b.startswith(prefix):
                return True
    return False


# ---------------------------------------------------------------------------
# Celebrity / whale filter
# ---------------------------------------------------------------------------

CELEBRITY_DENYLIST = {
    # Crypto-OG broadcast accounts that don't shill
    "elonmusk", "vitalikbuterin", "cz_binance", "saylor", "100trillionusd",
    "apompliano", "kobeissiletter", "9gagceo", "cryptorover", "ashcrypto",
    "justinsuntron", "brian_armstrong", "balajis", "michael_saylor",
    "jessepollak",  # Coinbase exec, broadcast-only at this point
    "punk6529",     # arguable — anon thought leader; kept here, override via PERSON_ALLOWLIST if not desired
    # Mainstream celebrities outside crypto-native space
    "parishilton", "diplo", "kevinrose", "garyvee",
    # Trader / news aggregator accounts that broadcast-only
    "zachboychuk", "raynft_", "styler_walker",
}

# Heuristic threshold: 1M+ followers AND friend-to-follower ratio < 0.0005
# captures accounts that broadcast >2000× more than they engage. Tunable.
CELEB_FOLLOWERS_FLOOR = 1_000_000
CELEB_RATIO_CAP = 0.0005


def is_celebrity(
    handle: str,
    followers: int | None,
    friends_count: int | None,
) -> bool:
    """True if the account is a broadcast-only / unreachable celebrity.

    Two layers:
      1. Hand-curated denylist (most reliable)
      2. Followers/follows ratio heuristic (catches new-to-bank whales)
    PERSON_ALLOWLIST overrides BOTH. Useful when the operator has direct
    contact with someone who passes the heuristic.
    """
    h = (handle or "").lower()
    if not h:
        return False
    if h in PERSON_ALLOWLIST:
        return False
    if h in CELEBRITY_DENYLIST:
        return True
    f = followers or 0
    fr = friends_count or 0
    if f >= CELEB_FOLLOWERS_FLOOR and f > 0 and (fr / f) < CELEB_RATIO_CAP:
        return True
    return False


# ---------------------------------------------------------------------------
# Convenience: combined screen
# ---------------------------------------------------------------------------

def is_outreachable(
    handle: str,
    archetypes: list[str],
    bio: str | None,
    followers: int | None,
    friends_count: int | None,
) -> tuple[bool, str | None]:
    """True if this candidate should appear in operator-facing deliverables.

    Returns ``(is_outreachable, reason_if_not)``. Reason useful for
    operator-facing reporting ("filtered: organization", "filtered:
    celebrity (Elon-class broadcast)", etc.).
    """
    if is_organization(handle, archetypes, bio):
        return False, "organization"
    if is_celebrity(handle, followers, friends_count):
        return False, "celebrity"
    return True, None
