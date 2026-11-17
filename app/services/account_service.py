"""
Data-principal rights under India's DPDP Act 2023: access (export) and
erasure of a user's own data.

Retention policy for erasure (approved by the product owner):
  * The profile and OTP / OTP-throttle documents are deleted.
  * Claimant cases that never reached the respondent (statuses below) are
    deleted outright, with their negotiation, document records, email logs
    and stored files.
  * Claimant cases that reached the respondent are kept, because they are
    also the other party's record and may be needed in legal proceedings.
    The claimant's phone is removed and their name replaced with
    "[erased]". Their email and city/state are kept.
  * Cases where the user is the respondent belong to the claimant and are
    not changed.
  * Cases filed by an organisation (``org_id`` set) belong to the
    organisation, even when its contact email is the user's: they are
    neither exported as the user's own nor changed.
  * The user is removed from every organisation's member list. Deletion is
    refused while they are the only owner of an organisation.
"""
import logging
from datetime import datetime, timezone

from app.models.case import CaseStatus
from app.services.blob_service import _TOP_LEVEL_URL_FIELDS, blob_service, refresh_case_urls
from app.services.cosmos_service import cosmos_service

logger = logging.getLogger(__name__)

# Statuses a case can only be in before the respondent was ever contacted.
ERASABLE_STATUSES = frozenset({
    CaseStatus.SUBMITTED.value,
    CaseStatus.ANALYZING.value,
    CaseStatus.ANALYZED.value,
    CaseStatus.ABANDONED.value,
    CaseStatus.CRIMINAL_ADVISORY.value,
})

class SoleOwnerError(Exception):
    """The user is the only owner of these organisations (list of names)."""

    def __init__(self, org_names: list[str]):
        super().__init__(", ".join(org_names))
        self.org_names = org_names


def _clean(doc: dict) -> dict:
    """Drop Cosmos system properties (_rid, _etag, …)."""
    return {k: v for k, v in doc.items() if not k.startswith("_")}


def _split_cases(email: str) -> tuple[list, list]:
    email = email.lower()
    as_claimant, as_respondent = [], []
    for c in cosmos_service.get_cases_by_email(email):
        if (c.get("claimant_email") or "").lower() == email:
            if not c.get("org_id"):  # organisation cases are the organisation's
                as_claimant.append(c)
        else:
            as_respondent.append(c)
    return as_claimant, as_respondent


# ── Export ────────────────────────────────────────────────────

def export_user_data(user: dict) -> dict:
    as_claimant, as_respondent = _split_cases(user["email"])
    respondent_views = []
    for c in as_respondent:
        # Never the full case: it holds the claimant's private analysis.
        view = cosmos_service.get_case_for_respondent(c["id"])
        if view:
            respondent_views.append(_clean(view))
    return {
        "export_format": "legalai-dpdp-export/1",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "profile": _clean(user),
        "cases_as_claimant": [_clean(refresh_case_urls(c)) for c in as_claimant],
        "cases_as_respondent": respondent_views,
        "organisation_memberships": [
            {"org_id": o["id"], "org_name": o.get("name"), **_clean(m)}
            for o in cosmos_service.get_orgs_for_member(user["email"])
            for m in o.get("members", []) if m.get("email") == user["email"].lower()
        ],
    }


# ── Erasure ───────────────────────────────────────────────────

def _case_files(case: dict) -> set[tuple[str, str]]:
    """Every stored file a case points at, as (container_key, blob_name)."""
    refs: set[tuple[str, str]] = set()
    for f in case.get("evidence_files") or []:
        if isinstance(f, dict):
            if f.get("blob_name"):
                refs.add(("evidence", f["blob_name"]))
            elif (ref := blob_service.parse_url(f.get("url"))):
                refs.add(ref)
    urls = [case.get(k) for k in _TOP_LEVEL_URL_FIELDS]
    docs = case.get("documents_data")
    if isinstance(docs, dict):
        urls += [v for k, v in docs.items() if k.endswith("_url") and isinstance(v, str)]
    urls += [d.get("blob_url") for d in cosmos_service.get_case_documents_all(case["id"])]
    for url in urls:
        if (ref := blob_service.parse_url(url)):
            refs.add(ref)
    return refs


def _delete_case_with_files(case: dict) -> tuple[int, int]:
    deleted = failed = 0
    for container_key, blob_name in sorted(_case_files(case)):
        try:
            blob_service.delete(container_key, blob_name)
            deleted += 1
        except Exception as e:
            # Already gone counts as deleted; anything else is logged for follow-up.
            if type(e).__name__ == "ResourceNotFoundError":
                deleted += 1
                continue
            failed += 1
            logger.error(
                f"Erasure: file delete failed | case_id={case['id']} | "
                f"container={container_key} | blob={blob_name} | error={e}"
            )
    cosmos_service.delete_case(case["id"])
    return deleted, failed


def _is_sole_owner(org: dict, email: str) -> bool:
    owners = [m["email"] for m in org.get("members", []) if m.get("role") == "owner"]
    return owners == [email]


def _leave_organisations(email: str) -> int:
    """Remove the user from every organisation; refuse if any would lose its last owner."""
    orgs = cosmos_service.get_orgs_for_member(email)
    blocked = [o.get("name") or o["id"] for o in orgs if _is_sole_owner(o, email)]
    if blocked:
        raise SoleOwnerError(blocked)

    def apply(org):
        if _is_sole_owner(org, email):  # an owner left concurrently
            raise SoleOwnerError([org.get("name") or org["id"]])
        org["members"] = [m for m in org.get("members", []) if m.get("email") != email]

    for org in orgs:
        cosmos_service.mutate_org(org["id"], apply)
    return len(orgs)


def erase_user(user: dict) -> dict:
    """Raises SoleOwnerError, before changing anything, if the user is an organisation's only owner."""
    email = user["email"].lower()
    orgs_left = _leave_organisations(email)
    as_claimant, as_respondent = _split_cases(email)

    deleted_cases, retained_cases = [], []
    files_deleted = files_failed = 0
    for case in as_claimant:
        if case.get("status") in ERASABLE_STATUSES:
            d, f = _delete_case_with_files(case)
            files_deleted += d
            files_failed += f
            deleted_cases.append(case["id"])
        else:
            cosmos_service.minimise_claimant_on_case(case["id"])
            retained_cases.append(case["id"])

    # Profile last: until it is gone the user's token still works, so a
    # failure above can simply be retried.
    cosmos_service.delete_user_data(email)

    logger.info(
        f"Account erased | deleted_cases={len(deleted_cases)} | "
        f"retained_cases={len(retained_cases)} | files_deleted={files_deleted} | "
        f"files_failed={files_failed}"
    )
    return {
        "deleted_case_ids": deleted_cases,
        "retained_case_ids": retained_cases,
        "respondent_case_count": len(as_respondent),
        "organisations_left": orgs_left,
        "files_deleted": files_deleted,
        "files_failed": files_failed,
    }
