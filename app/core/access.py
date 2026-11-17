"""
Who may act on a case.

A case's claimant side is either the individual who filed it, or — for
cases filed by an organisation — any member of that organisation.
Every claimant-side endpoint goes through these helpers so the rule lives
in one place.
"""
from app.core.exceptions import UnauthorizedAccess

ORG_ROLES = ("owner", "admin", "agent")
ADMIN_ROLES = ("owner", "admin")


def org_role(org: dict | None, email: str) -> str | None:
    if not org:
        return None
    email = (email or "").lower()
    for m in org.get("members", []):
        if m.get("email") == email:
            return m.get("role")
    return None


def is_claimant_side(case: dict, user: dict | None) -> bool:
    if not user:
        return False
    email = (user.get("email") or "").lower()
    if email and (case.get("claimant_email") or "").lower() == email:
        return True
    org_id = case.get("org_id")
    if org_id:
        from app.services.cosmos_service import cosmos_service
        return org_role(cosmos_service.get_org(org_id), email) in ORG_ROLES
    return False


def require_claimant_side(case: dict, user: dict | None) -> None:
    if not is_claimant_side(case, user):
        raise UnauthorizedAccess()


def is_party(case: dict, user: dict | None) -> bool:
    """Claimant side, or the respondent logged in with their own account."""
    if is_claimant_side(case, user):
        return True
    email = ((user or {}).get("email") or "").lower()
    return bool(email) and (case.get("respondent_email") or "").lower() == email


def require_party(case: dict, user: dict | None) -> None:
    if not is_party(case, user):
        raise UnauthorizedAccess()
