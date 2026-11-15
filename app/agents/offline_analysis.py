"""
Rule-based legal analysis used when no LLM is configured or an LLM call
fails. It is deliberately conservative: keyword classification, evidence
detection and a curated table of Indian statutes for the common dispute
categories. Output matches the LLM agents' schemas so every downstream
agent, document and UI screen works unchanged.

Criminal red flags are always checked so that violent / criminal matters
are never routed into mediation, even with the LLM unavailable.
"""
import re
from datetime import date, datetime

from app.core.legal_reference import (
    get_consumer_forum_tier,
    get_limitation_period,
    get_rent_control_act,
    CONSUMER_FORUM_TIERS,
)

RULE_BASED_NOTE = (
    "Rule-based analysis (no AI model configured). Treat as a starting point "
    "and verify with a licensed advocate."
)

# ── Criminal red flags (checked first, always) ─────────────────────────────
CRIMINAL_PATTERNS = {
    "physical violence":   r"\b(assault(ed)?|beat(en)?|hit me|slapped|punched|attacked|injur(ed|y))\b",
    "threats":             r"\b(threat\w* to (kill|harm|hurt|beat|attack)|kill me|death threats?|life threats?|criminal intimidation)\b",
    "sexual harassment":   r"\b(molest\w*|sexual(ly)? (assault|harass)\w*|rape)\b",
    "stalking":            r"\b(stalk(ed|ing)?|following me)\b",
    "domestic violence":   r"\b(domestic violence|dowry)\b",
    "cybercrime":          r"\b(hack(ed|ing)?|phishing|otp fraud|identity theft|morphed)\b",
    "forgery / extortion": r"\b(forg(ed|ery)|extort\w*|blackmail\w*)\b",
}

# ── Category rules: (category, track, label, keyword regex) ────────────────
CATEGORY_RULES = [
    ("pf_gratuity", "employment", "PF / Gratuity Non-payment",
     r"\b(gratuity|provident fund|\bpf\b|epf)\b"),
    ("wrongful_termination", "employment", "Wrongful Termination",
     r"\b(terminat\w+|fired|dismiss\w+|laid off|sacked)\b"),
    ("unpaid_salary", "employment", "Unpaid Salary / Wages",
     r"\b(salary|wages?|unpaid (pay|dues)|full and final|f&f|notice pay)\b"),
    ("rental_deposit", "monetary_civil", "Security Deposit Refund",
     r"\b(security deposit|deposit|landlord|tenant|rent(ed|al)?|lease|leave and licen[cs]e)\b"),
    ("cheque_bounce", "monetary_civil", "Cheque Dishonour",
     r"\b(cheque|check) (bounce\w*|dishonou?r\w*|return\w*)\b|\bbounced cheque\b"),
    ("loan_default", "monetary_civil", "Loan Recovery",
     r"\b(lent|loan(ed)?|borrow(ed)?|repay\w*)\b"),
    ("consumer_builder", "consumer", "Real Estate / Builder Delay",
     r"\b(builder|possession|flat booking|rera|apartment delivery)\b"),
    ("consumer_insurance", "consumer", "Insurance Claim Rejection",
     r"\b(insurance|insurer|claim (was )?(rejected|repudiated)|policy)\b"),
    ("consumer_general", "consumer", "Consumer Deficiency in Service",
     r"\b(refund|order(ed)?|deliver(y|ed)|defective|product|e-?commerce|warranty|service cent(er|re)|amazon|flipkart)\b"),
    ("contract_breach", "monetary_civil", "Breach of Contract",
     r"\b(contract|agreement|invoice|payment (due|pending)|freelanc\w+|client|vendor|advance)\b"),
    ("property_damage", "monetary_civil", "Property Damage",
     r"\b(damag(ed|e)|broke(n)?|leak(age)?|scratch\w*)\b"),
    ("defamation", "monetary_civil", "Defamation",
     r"\b(defam\w+|false (statements?|allegations?)|reputation)\b"),
    ("neighbour_dispute", "non_monetary", "Neighbour / Nuisance Dispute",
     r"\b(neighbou?r\w*|noise|boundary|encroach\w*|parking)\b"),
]

EVIDENCE_PATTERNS = {
    "Written agreement / contract":  r"\b(agreement|contract|lease|leave and licen[cs]e|offer letter|appointment letter)\b",
    "Payment proof (bank/UPI/receipt)": r"\b(receipt|bank (transfer|statement)|upi|neft|imps|paid via|transaction|invoice)\b",
    "Written communication (email/WhatsApp/SMS)": r"\b(e-?mails?|whatsapp|sms|messages?|chat|letter)\b",
    "Formal notice already sent":     r"\b(notice|legal notice|written notice)\b",
    "Photographs / video":            r"\b(photos?|photographs?|pictures?|videos?|recording)\b",
    "Salary slips / employment records": r"\b(salary slips?|payslips?|pay slips?|relieving letter|experience letter)\b",
    "Cheque / bank memo":             r"\b(cheque|return memo)\b",
    "Witnesses":                      r"\bwitness(es)?\b",
}

MISSING_PROOF_BY_CATEGORY = {
    "rental_deposit":       ["Rent / leave-and-licence agreement", "Proof of deposit payment",
                             "Move-out notice and key handover record", "Move-out photos of the premises"],
    "unpaid_salary":        ["Offer / appointment letter", "Salary slips and bank statements",
                             "Attendance or work records for the unpaid period", "Resignation/relieving correspondence"],
    "wrongful_termination": ["Appointment letter and service rules", "Termination letter or message",
                             "Performance records", "Proof of notice pay / dues not settled"],
    "pf_gratuity":          ["Service certificate showing years of service", "Last drawn salary slip",
                             "UAN / PF passbook", "Gratuity claim (Form I) acknowledgement"],
    "consumer_general":     ["Invoice / order confirmation", "Payment proof", "Complaint emails and ticket numbers",
                             "Photos of the defect / non-delivery proof"],
    "consumer_insurance":   ["Policy document", "Claim form and rejection letter", "Medical/repair bills",
                             "Grievance filed with the insurer"],
    "consumer_builder":     ["Builder-buyer agreement", "Payment receipts", "Promised possession date",
                             "RERA registration number of the project"],
    "cheque_bounce":        ["Original cheque", "Bank return memo", "Statutory demand notice (within 30 days)",
                             "Proof of underlying debt"],
    "loan_default":         ["Proof of transfer of the loan amount", "Any written acknowledgement of the debt",
                             "Messages demanding repayment"],
    "contract_breach":      ["Signed contract / purchase order", "Proof of your performance (deliverables)",
                             "Invoices and payment reminders"],
}

LAWS_BY_CATEGORY = {
    "rental_deposit": [
        ("Indian Contract Act, 1872", "Section 73",
         "Compensation for loss caused by the landlord's breach of the obligation to refund the deposit.", "PRIMARY"),
        ("{rent_act}", "Deposit / refund provisions",
         "State tenancy law governing security deposits and landlord-tenant obligations.", "SECONDARY"),
        ("Transfer of Property Act, 1882", "Section 108",
         "Rights and liabilities of lessor and lessee, including return of property and dues.", "SUPPORTING"),
    ],
    "unpaid_salary": [
        ("Code on Wages, 2019 (earlier: Payment of Wages Act, 1936)", "Wage payment and claims provisions",
         "Wages earned must be paid within the statutory time limit; claims lie before the wage authority.", "PRIMARY"),
        ("Industrial Disputes Act, 1947", "Section 33C(2)",
         "Recovery of money due from an employer through the Labour Court.", "SECONDARY"),
        ("Indian Contract Act, 1872", "Section 73",
         "Damages for breach of the employment contract.", "SUPPORTING"),
    ],
    "wrongful_termination": [
        ("Industrial Disputes Act, 1947", "Section 25F",
         "Conditions precedent to retrenchment: notice and retrenchment compensation.", "PRIMARY"),
        ("Indian Contract Act, 1872", "Section 73",
         "Damages, including notice pay, for termination in breach of contract.", "SECONDARY"),
    ],
    "pf_gratuity": [
        ("Payment of Gratuity Act, 1972", "Sections 4 and 7",
         "Gratuity payable after 5 years of continuous service; employer must pay within 30 days.", "PRIMARY"),
        ("Employees' Provident Funds and Miscellaneous Provisions Act, 1952", "Section 7A",
         "Determination and recovery of PF dues by the PF authority.", "SECONDARY"),
    ],
    "consumer_general": [
        ("Consumer Protection Act, 2019", "Section 2(11)",
         "Deficiency in service by the seller / service provider.", "PRIMARY"),
        ("Consumer Protection Act, 2019", "Section 2(47)",
         "Unfair trade practice.", "SECONDARY"),
        ("Consumer Protection Act, 2019", "Section 35",
         "Filing a complaint before the Consumer Commission (e-Daakhil online filing available).", "SUPPORTING"),
    ],
    "consumer_insurance": [
        ("Consumer Protection Act, 2019", "Section 2(11)",
         "Wrongful repudiation of a valid claim is a deficiency in service.", "PRIMARY"),
        ("Insurance Ombudsman Rules, 2017", "Rule 14",
         "Complaint to the Insurance Ombudsman after the insurer's grievance process.", "SECONDARY"),
    ],
    "consumer_builder": [
        ("Real Estate (Regulation and Development) Act, 2016", "Section 18",
         "Refund with interest or compensation for delay in possession.", "PRIMARY"),
        ("Consumer Protection Act, 2019", "Section 2(11)",
         "Delay in possession is a deficiency in service.", "SECONDARY"),
    ],
    "cheque_bounce": [
        ("Negotiable Instruments Act, 1881", "Section 138",
         "Dishonour of cheque for insufficiency of funds; demand notice within 30 days of the return memo.", "PRIMARY"),
        ("Negotiable Instruments Act, 1881", "Section 142",
         "Complaint to be filed within one month after the 15-day payment window lapses.", "SECONDARY"),
    ],
    "loan_default": [
        ("Indian Contract Act, 1872", "Section 73",
         "Recovery of the amount lent with damages for non-repayment.", "PRIMARY"),
        ("Code of Civil Procedure, 1908", "Order XXXVII",
         "Summary suit for recovery of a debt based on written evidence.", "SECONDARY"),
    ],
    "contract_breach": [
        ("Indian Contract Act, 1872", "Section 73",
         "Compensation for loss caused by breach of contract.", "PRIMARY"),
        ("Indian Contract Act, 1872", "Section 74",
         "Reasonable compensation where the contract names a sum for breach.", "SECONDARY"),
        ("Specific Relief Act, 1963", "Section 10",
         "Specific performance of the contract where damages are inadequate.", "SUPPORTING"),
    ],
    "property_damage": [
        ("Law of Torts", "Negligence / trespass to property",
         "Liability for loss caused to your property.", "PRIMARY"),
        ("Indian Contract Act, 1872", "Section 73",
         "Compensation where the damage arose from a contractual relationship.", "SECONDARY"),
    ],
    "defamation": [
        ("Law of Torts", "Civil defamation",
         "Damages and injunction for false statements harming reputation.", "PRIMARY"),
        ("Bharatiya Nyaya Sanhita, 2023", "Section 356 (earlier IPC 499/500)",
         "Criminal defamation — a separate remedy before the Magistrate.", "SUPPORTING"),
    ],
    "neighbour_dispute": [
        ("Law of Torts", "Private nuisance",
         "Unreasonable interference with the use and enjoyment of your property.", "PRIMARY"),
        ("Specific Relief Act, 1963", "Section 38",
         "Perpetual injunction to stop a continuing wrong.", "SECONDARY"),
    ],
    "criminal": [
        ("Bharatiya Nagarik Suraksha Sanhita, 2023", "Section 173 (earlier CrPC 154)",
         "Registration of an FIR for a cognizable offence — including Zero FIR at any police station.", "PRIMARY"),
        ("Bharatiya Nyaya Sanhita, 2023", "Relevant offence sections",
         "E.g. s.115 hurt, s.351 criminal intimidation, s.78 stalking, s.318 cheating.", "SECONDARY"),
        ("Information Technology Act, 2000", "Sections 66C / 66D",
         "Identity theft and cheating by impersonation using computer resources.", "SUPPORTING"),
    ],
}

# Criminal element -> sections to cite in the FIR (BNS in force since 1 July 2024).
CRIMINAL_SECTIONS = {
    "physical violence":   ["BNS S.115(2) voluntarily causing hurt (earlier IPC 323)",
                            "BNS S.118(1) hurt by dangerous weapon (earlier IPC 324)"],
    "threats":             ["BNS S.351 criminal intimidation (earlier IPC 506)"],
    "sexual harassment":   ["BNS S.75 sexual harassment (earlier IPC 354A)",
                            "BNS S.74 assault on a woman (earlier IPC 354)"],
    "stalking":            ["BNS S.78 stalking (earlier IPC 354D)"],
    "domestic violence":   ["BNS S.85 cruelty by husband or relatives (earlier IPC 498A)",
                            "Protection of Women from Domestic Violence Act 2005, S.12 and S.18"],
    "cybercrime":          ["IT Act 2000 S.66C identity theft", "IT Act 2000 S.66D cheating by personation",
                            "BNS S.318 cheating (earlier IPC 420)"],
    "forgery / extortion": ["BNS S.308 extortion (earlier IPC 384)", "BNS S.336 forgery (earlier IPC 465)"],
}

FORUM_BY_TRACK = {
    "monetary_civil": ("civil_court", "District Civil Court / Small Causes Court"),
    "employment":     ("labour_court", "Labour Court / Wage Authority"),
    "consumer":       ("consumer_forum", None),  # resolved by claim amount
    "non_monetary":   ("civil_court", "District Civil Court (or Lok Adalat)"),
    "criminal":       ("police", "Jurisdictional Police Station / Magistrate"),
}


def _search(pattern: str, text: str) -> bool:
    return re.search(pattern, text, re.IGNORECASE) is not None


def detect_criminal_elements(text: str) -> list:
    return [label for label, pat in CRIMINAL_PATTERNS.items() if _search(pat, text)]


def classify_category(text: str, respondent_type: str | None = None) -> tuple[str, str, str]:
    for category, track, label, pattern in CATEGORY_RULES:
        if _search(pattern, text):
            if track == "consumer" and respondent_type == "individual" and category == "consumer_general":
                continue
            return category, track, label
    return "other", "monetary_civil", "General Civil Dispute"


def extract_amount(text: str) -> float | None:
    m = re.search(
        r"(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d+)?)\s*(lakh|lac|crore)?",
        text, re.IGNORECASE,
    )
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    if unit in ("lakh", "lac"):
        value *= 100_000
    elif unit == "crore":
        value *= 10_000_000
    return value


def route(dispute_text: str, claim_amount, respondent_type: str, claimant_state: str) -> dict:
    criminal = detect_criminal_elements(dispute_text)
    category, track, _ = classify_category(dispute_text, respondent_type)
    if criminal:
        track = "criminal"
    amount = claim_amount or extract_amount(dispute_text)
    # Reputation / general disputes with no money claimed are about actions
    # (apology, removal, stopping conduct), so mediate them as non-monetary.
    if track == "monetary_civil" and not amount and category in ("defamation", "other"):
        track = "non_monetary"
    return {
        "track":                  track,
        "confidence":             0.6 if category != "other" else 0.35,
        "is_criminal":            bool(criminal),
        "is_monetary":            bool(amount) and track != "non_monetary",
        "respondent_is_company":  respondent_type == "company",
        "dispute_category":       "criminal" if criminal else category,
        "jurisdiction_state":     claimant_state,
        "estimated_claim_amount": amount,
        "criminal_elements":      criminal,
        "routing_notes":          (
            f"Rule-based routing: criminal elements detected ({', '.join(criminal)})"
            if criminal else f"Rule-based routing by keywords → {category}"
        ),
        "analysis_mode":          "rule_based",
    }


# Which detected evidence type satisfies which missing-proof item.
_COVERS = [
    ("Written agreement / contract",      r"agreement|contract|offer|appointment|policy document"),
    ("Payment proof (bank/UPI/receipt)",  r"payment|receipt|bank|invoice"),
    ("Formal notice already sent",        r"notice"),
    ("Photographs / video",               r"photo"),
    ("Salary slips / employment records", r"salary slip|relieving"),
    ("Written communication (email/WhatsApp/SMS)", r"emails|messages|correspondence"),
    ("Cheque / bank memo",                r"cheque|return memo"),
]


def _still_missing(items: list, evidence: list) -> list:
    out = []
    for item in items:
        covered = any(
            label in evidence and re.search(pat, item, re.IGNORECASE)
            for label, pat in _COVERS
        )
        if not covered:
            out.append(item)
    return out


def _sentences(text: str, limit: int = 5) -> list:
    parts = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 15]
    return parts[:limit]


def _evidence(text: str) -> list:
    return [label for label, pat in EVIDENCE_PATTERNS.items() if _search(pat, text)]


def _years_since(date_str: str | None) -> float | None:
    if not date_str:
        return None
    try:
        d = datetime.fromisoformat(date_str[:10]).date()
    except ValueError:
        return None
    return (date.today() - d).days / 365.25


def intake(case: dict, track: str) -> dict:
    text = case.get("dispute_text", "")
    routing = case.get("routing") or {}
    category = routing.get("dispute_category")
    if not category or category == "other" or routing.get("analysis_mode") != "rule_based":
        category, _, _ = classify_category(text, case.get("respondent_type"))
    label = next((l for c, _, l, _ in CATEGORY_RULES if c == category), "General Civil Dispute")
    if track == "criminal":
        label = "Criminal Matter (advisory only)"

    evidence = _evidence(text) + [
        f"Uploaded file: {f.get('filename')}" for f in case.get("evidence_files", []) or []
    ]
    score = min(90, 25 + 12 * len(evidence))
    missing = _still_missing(
        MISSING_PROOF_BY_CATEGORY.get(category, ["Any written record of the agreement and payments"]),
        evidence,
    ) or ["Keep originals and back-ups of everything you described"]

    limitation_years = get_limitation_period(category)
    elapsed = _years_since(case.get("incident_date"))
    time_barred = elapsed is not None and elapsed > limitation_years

    amount = case.get("claim_amount") or extract_amount(text)
    strengths = []
    if evidence:
        strengths.append(f"Documentary support mentioned: {', '.join(evidence[:3])}")
    if _search(r"\b(notice|informed|repeated(ly)?|followed up|reminder)\b", text):
        strengths.append("You raised the issue with the other side before escalating")
    if amount:
        strengths.append(f"The claim is for a specific, quantified amount (Rs. {amount:,.0f})")

    weaknesses = []
    if len(evidence) < 2:
        weaknesses.append("Limited documentary evidence described — gather written proof")
    if time_barred:
        weaknesses.append(f"Claim may be beyond the {limitation_years}-year limitation period")
    if not case.get("incident_date"):
        weaknesses.append("Incident date not provided — needed to confirm limitation")

    severity = "HIGH" if track == "criminal" or (amount or 0) >= 500_000 else (
        "MEDIUM" if (amount or 0) >= 25_000 else "LOW")

    return {
        "dispute_type":            label,
        "dispute_category":        category,
        "track_confirmed":         track,
        "severity":                severity,
        "key_facts":               _sentences(text) or [text[:200]],
        "parties_summary":         f"{case.get('claimant_name')} vs {case.get('respondent_name')}",
        "incident_date_confirmed": case.get("incident_date"),
        "jurisdiction_state":      case.get("claimant_state", "India"),
        "jurisdiction_city":       case.get("claimant_city"),
        "evidence_available":      evidence,
        "evidence_strength_score": score,
        "missing_proof_checklist": missing,
        "is_time_barred":          time_barred,
        "limitation_notes":        f"Typical limitation period for this dispute: {limitation_years} year(s).",
        "respondent_is_company":   case.get("respondent_type") == "company",
        "key_issues":              [label, "Whether the respondent's refusal / non-payment is justified"],
        "claimant_strengths":      strengths,
        "claimant_weaknesses":     weaknesses,
        "confirmed_claim_amount":  amount,
        "currency":                case.get("currency", "INR"),
        "recommended_approach":    "fir_advisory" if track == "criminal" else "mediation",
        "analysis_mode":           "rule_based",
        "analysis_note":           RULE_BASED_NOTE,
    }


def _criminal_sections(case: dict) -> list:
    elements = (case.get("routing") or {}).get("criminal_elements") \
        or detect_criminal_elements(case.get("dispute_text", ""))
    sections = []
    for el in elements:
        for sec in CRIMINAL_SECTIONS.get(el, []):
            if sec not in sections:
                sections.append(sec)
    return sections


def legal(case: dict, track: str, intake_data: dict | None = None) -> dict:
    intake_data = intake_data or {}
    category = "criminal" if track == "criminal" else intake_data.get("dispute_category", "other")
    state = case.get("claimant_state") or "India"

    laws = [
        {"act": act.format(rent_act=get_rent_control_act(state)), "section": sec,
         "relevance": rel, "strength": strength}
        for act, sec, rel, strength in LAWS_BY_CATEGORY.get(category, LAWS_BY_CATEGORY["contract_breach"])
    ]

    forum, forum_name = FORUM_BY_TRACK.get(track, FORUM_BY_TRACK["monetary_civil"])
    if track == "consumer":
        forum_name = CONSUMER_FORUM_TIERS[get_consumer_forum_tier(case.get("claim_amount"))]["description"]
    if case.get("claimant_city") and forum_name:
        forum_name = f"{forum_name}, {case['claimant_city']}"

    score = intake_data.get("evidence_strength_score", 40)
    if intake_data.get("is_time_barred"):
        standing = "WEAK"
    elif score >= 75:
        standing = "STRONG"
    elif score >= 50:
        standing = "MODERATE"
    else:
        standing = "WEAK"

    limitation = get_limitation_period(category if category != "criminal" else "other")

    if track == "criminal":
        immediate = ["Report to the police (dial 112 in an emergency) and request an FIR",
                     "Seek medical examination and keep records if injured",
                     "Preserve messages, call logs and any CCTV footage"]
        relief = ["Registration of FIR and investigation", "Protection orders where applicable"]
        steps = ["Visit the jurisdictional police station or file a Zero FIR",
                 "If the FIR is refused, approach the Superintendent of Police in writing",
                 "If still refused, apply to the Magistrate under BNSS Section 175(3)"]
    else:
        immediate = ["Send a written legal notice giving 15 days to comply",
                     "Collect and back up all documents listed in the missing-proof checklist",
                     "Keep all further communication in writing"]
        relief = ["Recovery of the amount claimed", "Interest on delayed payment", "Costs of proceedings"]
        steps = ["Send a legal notice", "Attempt mediation (this platform / Lok Adalat)",
                 f"File before the {forum_name or 'appropriate forum'} if unresolved"]

    return {
        "track":                   track,
        "applicable_laws":         laws,
        "legal_standing":          standing,
        "legal_standing_reason":   (
            f"Based on {len(intake_data.get('evidence_available', []))} type(s) of evidence described "
            f"(evidence score {score}/100). {RULE_BASED_NOTE}"
        ),
        "jurisdiction_state":      state,
        "forum":                   forum,
        "forum_name":              forum_name,
        "limitation_period_years": limitation,
        "is_within_limitation":    not intake_data.get("is_time_barred", False),
        "limitation_notes":        intake_data.get("limitation_notes"),
        "claimant_rights":         [law["relevance"] for law in laws[:2]],
        "respondent_defenses":     [
            "May dispute the facts or the amount claimed",
            "May claim deductions or set-off (e.g. damages, dues owed)",
            "May argue the claim is time-barred or lacks documentary proof",
        ],
        "key_legal_issues":        intake_data.get("key_issues", []),
        "precedent_cases":         [],
        "legal_notice_required":   track != "criminal",
        "legal_notice_notice_period_days": 15,
        "relief_available":        relief,
        "filing_steps":            steps,
        "recommended_immediate_actions": immediate,
        "total_statutory_dues":    0,
        "statutory_entitlements":  {},
        "ipc_sections":            _criminal_sections(case) if track == "criminal" else [],
        "analysis_mode":           "rule_based",
    }


STANDING_WIN_PROBABILITY = {"VERY_STRONG": 80, "STRONG": 70, "MODERATE": 55, "WEAK": 35}


def win_probability(legal_standing: str, evidence_score: int) -> int:
    base = STANDING_WIN_PROBABILITY.get(legal_standing, 50)
    adjust = (evidence_score - 50) // 10 * 2
    return max(10, min(90, base + adjust))
