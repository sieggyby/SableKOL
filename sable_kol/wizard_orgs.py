"""Wizard-specific org-row helpers.

The any-project KOL wizard (`docs/any_project_wizard_plan.md`) submits a job
keyed by a fresh org_id derived from the operator-typed slug. ``jobs.org_id``
has a FOREIGN KEY to ``orgs(org_id)``, so the wizard MUST upsert the org row
in the same transaction it inserts the job.

Schema reality (verified against ``SablePlatform/sable_platform/db/migrations/001_initial.sql``):
the ``orgs`` table has columns ``org_id, display_name, discord_server_id,
twitter_handle, config_json, status, created_at, updated_at``. There is NO
``org_type`` column and NO ``is_active`` column — Codex round-2 #3 caught
this. Wizard-created prospects therefore use:

    status        = 'inactive'   (operator promotes via `sable-platform org config set`)
    config_json   = {"org_type": "prospect", "created_via": "kol_wizard",
                     "wizard_job_id": "<uuid>"}

That keeps the schema honest and lets `sable-platform org list --status inactive`
surface wizard-created prospects without a new column.

**Canonical prospect-org creator (B-N1):** the SP-owned
``sable_platform.db.orgs.upsert_prospect_org`` is now the canonical creator
(same convention: ``status='inactive'`` + ``config_json.org_type='prospect'``).
This wizard variant is kept deliberately rather than delegated because it
**force-refreshes ``wizard_job_id`` on re-run** (the audit trail must point at the
latest job), whereas the generic helper intentionally ``setdefault``s config keys to
preserve operator-set state. Unify only if the generic helper grows a "force these
keys" option. (The production wizard prospect-org INSERT is the TS mirror in
``SableWeb/src/lib/kol-create-job.ts`` — neither this nor the generic helper unifies it.)
"""
from __future__ import annotations

import json
from typing import Any


def upsert_wizard_org(
    conn: Any,
    *,
    org_id: str,
    display_name: str,
    twitter_handle: str | None,
    wizard_job_id: str,
) -> None:
    """Insert (or update) an org row for a wizard-created prospect.

    Idempotent: if the org already exists (operator re-ran the wizard with
    the same slug for a prospect they later promoted), we leave the existing
    status/config alone and only refresh ``twitter_handle`` and the wizard
    job link inside config_json so the audit trail stays accurate.
    """
    existing = conn.execute(
        "SELECT config_json, status FROM orgs WHERE org_id = :id",
        {"id": org_id},
    ).fetchone()

    if existing is None:
        conn.execute(
            "INSERT INTO orgs (org_id, display_name, twitter_handle, status, config_json) "
            "VALUES (:org_id, :display_name, :twitter_handle, 'inactive', :config_json)",
            {
                "org_id": org_id,
                "display_name": display_name,
                "twitter_handle": twitter_handle,
                "config_json": json.dumps(
                    {
                        "org_type": "prospect",
                        "created_via": "kol_wizard",
                        "wizard_job_id": wizard_job_id,
                    }
                ),
            },
        )
        conn.commit()
        return

    # Org exists — preserve operator-set status/config, refresh wizard pointer.
    cfg = json.loads(existing["config_json"] or "{}")
    cfg["wizard_job_id"] = wizard_job_id
    cfg.setdefault("created_via", "kol_wizard")
    cfg.setdefault("org_type", "prospect")
    conn.execute(
        "UPDATE orgs SET "
        "  twitter_handle = COALESCE(:twitter_handle, twitter_handle), "
        "  config_json = :config_json, "
        "  updated_at = CURRENT_TIMESTAMP "
        "WHERE org_id = :org_id",
        {
            "org_id": org_id,
            "twitter_handle": twitter_handle,
            "config_json": json.dumps(cfg),
        },
    )
    conn.commit()
