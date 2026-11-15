"""
B2B domain logic: plans, dispute templates and bulk-row validation.

A business (landlord, lender, seller, ...) files many similar disputes. Each
bulk row carries the facts that vary — who, how much, which invoice/loan —
and a template turns them into a proper dispute narrative, so cases are
consistent and the analysis agents get well-formed input.
"""
import csv
import io
import re
from datetime import date, datetime, timezone

from pydantic import ValidationError

from app.models.case import DisputeSubmission

# ── Plans ──────────────────────────────────────────────────────────────────
# Cases an organisation may file per calendar month. None = unlimited.
PLANS = {
    "free":       {"monthly_cases": 25,    "label": "Free"},
    "starter":    {"monthly_cases": 500,   "label": "Starter"},
    "growth":     {"monthly_cases": 5000,  "label": "Growth"},
    "enterprise": {"monthly_cases": None,  "label": "Enterprise"},
}
DEFAULT_PLAN = "free"
MAX_BULK_ROWS = 500


def month_key(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m")


def cases_this_month(cases: list) -> int:
    key = month_key()
    return sum(1 for c in cases if str(c.get("created_at", "")).startswith(key))


def remaining_quota(org: dict, cases: list) -> int | None:
    limit = PLANS.get(org.get("plan", DEFAULT_PLAN), PLANS[DEFAULT_PLAN])["monthly_cases"]
    if limit is None:
        return None
    return max(0, limit - cases_this_month(cases))


# ── Templates ──────────────────────────────────────────────────────────────
# Placeholders: {claimant} {respondent} {amount} {reference} {incident_date}
# {due_date} {details}. Organisations can add their own (stored on the org).
BUILTIN_TEMPLATES = {
    "overdue_invoice": {
        "title": "Overdue invoice",
        "text": ("{claimant} supplied goods or services to {respondent} and raised invoice {reference} "
                 "dated {incident_date} for Rs. {amount}. Payment was due on {due_date} but remains unpaid "
                 "despite reminders sent to {respondent}. {claimant} seeks payment of the outstanding "
                 "Rs. {amount} under the contract. {details}"),
    },
    "unpaid_rent": {
        "title": "Unpaid rent",
        "text": ("{respondent} occupies premises let by {claimant} under rent agreement {reference}. "
                 "Unpaid rent of Rs. {amount} is outstanding for the period ending {due_date}, despite "
                 "written reminders to {respondent}. {claimant} seeks payment of the rent arrears of "
                 "Rs. {amount}. {details}"),
    },
    "loan_default": {
        "title": "Loan instalments overdue",
        "text": ("{respondent} borrowed money from {claimant} under loan account {reference}, disbursed on "
                 "{incident_date}. Repayment instalments due up to {due_date} have not been paid and Rs. "
                 "{amount} is outstanding, despite repayment reminders. {claimant} seeks repayment of the "
                 "outstanding Rs. {amount} under the loan agreement. {details}"),
    },
    "cheque_bounce": {
        "title": "Dishonoured cheque",
        "text": ("{respondent} issued a cheque to {claimant} for Rs. {amount} towards an existing debt "
                 "(reference {reference}). The bounced cheque was returned unpaid by the bank on "
                 "{due_date}. {claimant} seeks payment of Rs. {amount}. {details}"),
    },
}

REQUIRED_FIELDS = ("respondent_name", "respondent_email", "reference")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class _Blank(dict):
    def __missing__(self, key):
        return "not stated"


def render_template(text: str, values: dict) -> str:
    out = text.format_map(_Blank({k: v for k, v in values.items() if v not in (None, "")}))
    return re.sub(r"\s+", " ", out.replace("not stated.", "").replace(" .", ".")).strip()


def _amount(value) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^\d.]", "", str(value))
    return float(cleaned) if cleaned else None


def _date(value) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date '{text}' (use YYYY-MM-DD or DD/MM/YYYY)")


def build_case_data(org: dict, row: dict, default_template: str | None) -> dict:
    """
    Validate one bulk row and return the case document fields.
    Raises ValueError with a message suitable for the per-row error report.
    """
    row = {str(k).strip().lower(): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}
    missing = [f for f in REQUIRED_FIELDS if not row.get(f)]
    if missing:
        raise ValueError(f"Missing required field(s): {', '.join(missing)}")
    email = row["respondent_email"].lower()
    if not _EMAIL.match(email):
        raise ValueError(f"Invalid respondent_email '{row['respondent_email']}'")
    if email == (org.get("contact_email") or "").lower():
        raise ValueError("Respondent email is the organisation's own contact email")

    amount = _amount(row.get("claim_amount") or row.get("amount"))
    if amount is not None and amount <= 0:
        raise ValueError("claim_amount must be positive")
    incident = _date(row.get("incident_date"))
    due = _date(row.get("due_date"))
    if incident and incident > date.today().isoformat():
        raise ValueError("incident_date is in the future")

    profile = org.get("profile") or {}
    claimant = profile.get("legal_name") or org.get("name")
    text = row.get("dispute_text")
    if not text:
        key = row.get("template") or default_template
        templates = {**BUILTIN_TEMPLATES, **(org.get("templates") or {})}
        if not key or key not in templates:
            raise ValueError("Provide dispute_text or a known template "
                             f"({', '.join(sorted(templates))})")
        text = render_template(templates[key]["text"], {
            "claimant": claimant, "respondent": row["respondent_name"],
            "amount": f"{amount:,.0f}" if amount else None, "reference": row["reference"],
            "incident_date": incident, "due_date": due, "details": row.get("details"),
        })

    data = {
        "dispute_text":            text,
        "claimant_name":           claimant,
        "claimant_email":          org.get("contact_email"),
        "claimant_phone":          profile.get("phone") or "",
        "claimant_state":          profile.get("state") or "",
        "claimant_city":           profile.get("city") or "",
        "respondent_name":         row["respondent_name"],
        "respondent_email":        email,
        "respondent_phone":        row.get("respondent_phone") or None,
        "respondent_type":         (row.get("respondent_type") or "individual").lower(),
        "respondent_company_name": row.get("respondent_company_name") or None,
        "incident_date":           incident,
        "claim_amount":            amount,
        "currency":                "INR",
        "claimant_consent":        True,   # accepted by the organisation's terms
        "disclaimer_acknowledged": True,
    }
    try:
        validated = DisputeSubmission(**data).model_dump()
    except ValidationError as e:
        first = e.errors()[0]
        raise ValueError(f"{'.'.join(str(x) for x in first['loc'])}: {first['msg']}")
    validated.update({"external_reference": str(row["reference"]), "due_date": due})
    return validated


def parse_csv(raw: bytes) -> list[dict]:
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")
    return [row for row in reader if any((v or "").strip() for v in row.values())]
