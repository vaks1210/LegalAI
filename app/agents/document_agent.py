import inspect
import io
from xml.sax.saxutils import escape as xml_escape, unescape as xml_unescape
import logging
from datetime import datetime, timezone
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer,
    HRFlowable, Table, TableStyle, KeepTogether,
)
from app.services.openai_service import log_llm_fallback, openai_service
from app.services.blob_service import blob_service
from app.services.cosmos_service import cosmos_service

logger = logging.getLogger(__name__)

# ── Palette ───────────────────────────────────────────────────
NAVY   = colors.HexColor("#0F2A4A")
BLUE   = colors.HexColor("#1565C0")
CYAN   = colors.HexColor("#06B6D4")
RED    = colors.HexColor("#DC2626")
GREEN  = colors.HexColor("#16A34A")
ORANGE = colors.HexColor("#EA580C")
DARK   = colors.HexColor("#1E293B")
MUTED  = colors.HexColor("#64748B")
LGREY  = colors.HexColor("#F1F5F9")
WHITE  = colors.white

W, H   = A4


# ══════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════

async def run_document_agent(case: dict) -> dict:
    case_id   = case["id"]
    track     = case.get("track", "monetary_civil")
    intake    = case.get("intake_data", {})
    legal     = case.get("legal_data", {})
    analytics = case.get("analytics_data", {})

    result = {
        "demand_letter_url":         None,
        "court_file_url":            None,
        "fir_advisory_url":          None,
        "settlement_url":            None,
        "mediation_certificate_url": None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    async def build(doc_type: str, result_key: str, make, expiry_hours: int, blob_prefix: str = None):
        # Each document is independent: one failure must not skip the rest.
        try:
            pdf_bytes = await make() if inspect.iscoroutinefunction(make) else make()
            blob_name = f"{blob_prefix or doc_type}_{case_id}.pdf"
            blob_service.upload("pdfs", blob_name, pdf_bytes)
            url = blob_service.generate_download_url("pdfs", blob_name, expiry_hours=expiry_hours)
            result[result_key] = url
            cosmos_service.save_document_record(case_id, doc_type, url)
            logger.info(f"{doc_type} generated | case_id={case_id}")
        except Exception as e:
            logger.exception(f"Document Agent failed | doc={doc_type} | case_id={case_id} | error={e}")
            result.setdefault("errors", {})[doc_type] = str(e)

    if track != "criminal":
        async def demand():
            return await _generate_demand_letter(case, intake, legal, analytics)
        await build("demand_letter", "demand_letter_url", demand, 168)
    if track in ["monetary_civil", "employment", "consumer"]:
        async def court():
            return await _generate_court_file(case, intake, legal, analytics)
        await build("court_file", "court_file_url", court, 168)
    if track == "criminal":
        async def fir():
            return await _generate_fir_advisory(case, intake, legal)
        await build("fir_advisory", "fir_advisory_url", fir, 168)
    if track != "criminal":
        await build("mediation_certificate", "mediation_certificate_url",
                    lambda: _generate_mediation_certificate(case), 720, blob_prefix="mediation_cert")

    return result


# ══════════════════════════════════════════════════════════════
# SETTLEMENT AGREEMENT — called separately on both-accept
# ══════════════════════════════════════════════════════════════

async def generate_settlement_agreement(case: dict, settled_amount: float) -> str:
    """
    Called by Negotiation router when both parties accept.
    Returns download URL.
    """
    case_id   = case["id"]
    track     = case.get("track", "monetary_civil")
    intake    = case.get("intake_data", {})
    legal     = case.get("legal_data", {})

    pdf_bytes = await _generate_settlement_pdf(
        case, intake, legal, settled_amount, track
    )
    blob_name = f"settlement_{case_id}.pdf"
    blob_service.upload("signed", blob_name, pdf_bytes)
    url = blob_service.generate_download_url("signed", blob_name, expiry_hours=720)
    cosmos_service.save_document_record(case_id, "settlement_agreement", url)
    cosmos_service.update_case(case_id, {"settlement_url": url})
    logger.info(f"Settlement agreement generated | case_id={case_id}")
    return url


# ══════════════════════════════════════════════════════════════
# BREACH OF SETTLEMENT NOTICE — called when payment not honored
# ══════════════════════════════════════════════════════════════

async def generate_breach_notice(case: dict) -> str:
    """Called when claimant reports settlement was not honored."""
    case_id   = case["id"]
    pdf_bytes = await _generate_breach_notice_pdf(case)
    blob_name = f"breach_notice_{case_id}.pdf"
    blob_service.upload("pdfs", blob_name, pdf_bytes)
    url = blob_service.generate_download_url("pdfs", blob_name, expiry_hours=720)
    cosmos_service.save_document_record(case_id, "breach_notice", url)
    cosmos_service.update_case(case_id, {
        "breach_notice_sent": True,
        "breach_notice_url": url,
    })
    logger.info(f"Breach notice generated | case_id={case_id}")
    return url


# ══════════════════════════════════════════════════════════════
# PDF HELPERS — shared utilities
# ══════════════════════════════════════════════════════════════

def _safe(text: str) -> str:
    """
    Convert all problematic Unicode characters to ASCII-safe equivalents.
    ReportLab Helvetica cannot render most Unicode.
    """
    if not text:
        return ""
    if isinstance(text, (list, tuple)):
        text = " ".join(str(t) for t in text if t)
    elif isinstance(text, dict):
        text = " ".join(str(v) for v in text.values() if v)
    elif not isinstance(text, str):
        text = str(text)
    replacements = {
        "\u20b9": "Rs.", "\u20ac": "EUR", "\u00a3": "GBP",
        "\u2019": "'",   "\u2018": "'",   "\u201c": '"',   "\u201d": '"',
        "\u2013": "-",   "\u2014": "--",  "\u2026": "...", "\u00a0": " ",
        "\u2022": "-",   "\u2192": "->",  "\u2714": "YES", "\u2718": "NO",
        "\u00b7": ".",   "\u2015": "--",
    }
    for char, replacement in replacements.items():
        text = text.replace(char, replacement)
    # Replace any remaining non-ASCII (previously done via "?" substitution,
    # which also erased genuine question marks).
    text = "".join(c if ord(c) < 128 else " " for c in text)
    # ReportLab Paragraph parses its input as markup: user text containing
    # "<" or "&" could break or inject formatting, crashing PDF generation.
    return xml_escape(text)


def _as_list(value) -> list:
    """LLM content fields meant to be lists sometimes arrive as a string/None."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return [value]


def _fmt_date(iso: str | None) -> str:
    """Format a stored ISO timestamp like _today(); fall back to today."""
    if iso:
        try:
            return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).strftime("%d %B %Y")
        except ValueError:
            pass
    return _today()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%d %B %Y")


def _make_doc(buf: io.BytesIO, margins: dict = None) -> SimpleDocTemplate:
    m = margins or {"left": 20*mm, "right": 20*mm, "top": 15*mm, "bottom": 15*mm}
    return SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=m["left"], rightMargin=m["right"],
        topMargin=m["top"],   bottomMargin=m["bottom"],
    )


def _header_block(theme_color, title: str, subtitle: str, case_id: str) -> list:
    """Colored header box used by all PDFs."""
    story = []
    # Title bar
    data  = [[Paragraph(
        _safe(title),
        ParagraphStyle("HT", fontName="Helvetica-Bold", fontSize=16,
                       textColor=WHITE, alignment=TA_CENTER),
    )]]
    tbl   = Table(data, colWidths=[170*mm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), theme_color),
        ("TOPPADDING",    (0,0), (-1,-1), 10),
        ("BOTTOMPADDING", (0,0), (-1,-1), 10),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 4*mm))
    story.append(Paragraph(
        _safe(subtitle),
        ParagraphStyle("HS", fontName="Helvetica-Oblique", fontSize=9,
                       textColor=MUTED, alignment=TA_CENTER),
    ))
    story.append(Paragraph(
        f"Case Ref: {case_id[:8].upper()}  |  Generated: {_today()}",
        ParagraphStyle("HC", fontName="Helvetica", fontSize=8,
                       textColor=MUTED, alignment=TA_CENTER),
    ))
    story.append(HRFlowable(width="100%", thickness=1.5,
                             color=theme_color, spaceAfter=6))
    return story


def _parties_table(case: dict) -> Table:
    """Two-column parties info table."""
    body  = ParagraphStyle("TB", fontName="Helvetica", fontSize=9, textColor=DARK)
    label = ParagraphStyle("TL", fontName="Helvetica-Bold", fontSize=8,
                            textColor=MUTED)
    data  = [
        [Paragraph("CLAIMANT", label), Paragraph("RESPONDENT", label)],
        [
            Paragraph(_safe(case["claimant_name"]), body),
            Paragraph(_safe(case["respondent_name"]), body),
        ],
        [
            Paragraph(_safe(f"{case['claimant_city']}, {case['claimant_state']}"), body),
            Paragraph(_safe(
                case.get("respondent_company_name") or
                f"{(case.get('respondent_type') or 'individual').title()}"
            ), body),
        ],
        [
            Paragraph(_safe(case["claimant_email"]), body),
            Paragraph(_safe(case["respondent_email"]), body),
        ],
    ]
    tbl = Table(data, colWidths=[85*mm, 85*mm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0),   LGREY),
        ("FONTNAME",      (0,0), (-1,0),   "Helvetica-Bold"),
        ("FONTSIZE",      (0,0), (-1,0),   8),
        ("GRID",          (0,0), (-1,-1),  0.4, colors.HexColor("#E2E8F0")),
        ("TOPPADDING",    (0,0), (-1,-1),  4),
        ("BOTTOMPADDING", (0,0), (-1,-1),  4),
        ("LEFTPADDING",   (0,0), (-1,-1),  6),
    ]))
    return tbl


def _section_heading(text: str, color=NAVY) -> Paragraph:
    return Paragraph(
        _safe(text.upper()),
        ParagraphStyle("SH", fontName="Helvetica-Bold", fontSize=9,
                       textColor=color, spaceBefore=8, spaceAfter=3,
                       borderPadding=(4, 0, 4, 0)),
    )


def _body_para(text: str) -> Paragraph:
    return Paragraph(
        _safe(text),
        ParagraphStyle("BP", fontName="Helvetica", fontSize=9,
                       textColor=DARK, leading=14, spaceAfter=4,
                       alignment=TA_JUSTIFY),
    )


def _bullet(text: str) -> Paragraph:
    return Paragraph(
        f"• {_safe(text)}",
        ParagraphStyle("BL", fontName="Helvetica", fontSize=9,
                       textColor=DARK, leading=13, leftIndent=8,
                       spaceAfter=2),
    )


def _disclaimer_footer() -> list:
    """Adds disclaimer footer to every PDF."""
    from app.core.disclaimer import AI_DISCLAIMER_TEXT, PDF_WATERMARK_TEXT

    return [
        Spacer(1, 6*mm),
        HRFlowable(width="100%", thickness=0.5, color=MUTED),
        Paragraph(
            f"⚠ {PDF_WATERMARK_TEXT}",
            ParagraphStyle("WM", fontName="Helvetica-Bold", fontSize=7,
                           textColor=colors.HexColor("#DC2626"),
                           alignment=TA_CENTER, spaceBefore=4, spaceAfter=2),
        ),
        Paragraph(
            _safe(AI_DISCLAIMER_TEXT),
            ParagraphStyle("DIS", fontName="Helvetica-Oblique", fontSize=7,
                           textColor=MUTED, alignment=TA_CENTER,
                           leading=10, spaceAfter=4),
        ),
    ]


def _append_case_record_sections(story: list, case: dict, intake: dict, legal: dict,
                                 analytics: dict | None = None, shared: bool = False):
    """
    shared=True for documents the respondent receives (demand letter,
    settlement agreement): omit the claimant's phone and the claimant's
    private analysis (weaknesses, evidence gaps, anticipated defences).
    Previously these were printed in the demand letter sent to the
    opposing party.
    """
    story.append(_section_heading("Case Record" if shared else "Full Case Record"))
    phone = "" if shared else f" | Phone: {case.get('claimant_phone', 'N/A')}"
    story.append(_body_para(
        f"Claimant: {case.get('claimant_name')} | Email: {case.get('claimant_email')}"
        f"{phone} | City/State: {case.get('claimant_city', 'N/A')}, {case.get('claimant_state', 'N/A')}"
    ))
    story.append(_body_para(
        f"Respondent: {case.get('respondent_name')} | Email: {case.get('respondent_email')} | "
        f"Phone: {case.get('respondent_phone', 'N/A')} | Type: {case.get('respondent_type', 'N/A')}"
    ))
    story.append(_body_para(
        f"Track: {case.get('track', 'N/A')} | Claim: "
        f"{('Rs. ' + format(case['claim_amount'], ',.0f')) if case.get('claim_amount') else 'Non-monetary'} | "
        f"Incident Date: {case.get('incident_date') or 'Not specified'}"
    ))
    story.append(_section_heading("Dispute Narrative"))
    story.append(_body_para(case.get("dispute_text", "")))

    if shared:
        evidence_files = case.get("evidence_files", [])
        if evidence_files:
            story.append(_section_heading("Evidence on Record"))
            for file_info in evidence_files[:12]:
                story.append(_bullet(f"{file_info.get('filename')} (uploaded {str(file_info.get('uploaded_at', ''))[:10]})"))
        return

    strengths = intake.get("claimant_strengths", [])
    weaknesses = intake.get("claimant_weaknesses", [])
    missing = intake.get("missing_proof_checklist", []) or analytics.get("missing_evidence", []) if analytics else intake.get("missing_proof_checklist", [])
    defenses = legal.get("respondent_defenses", [])
    if strengths:
        story.append(_section_heading("Claimant Strengths"))
        for item in strengths[:8]:
            story.append(_bullet(item))
    if weaknesses:
        story.append(_section_heading("Claimant Weaknesses"))
        for item in weaknesses[:8]:
            story.append(_bullet(item))
    if defenses:
        story.append(_section_heading("Likely Respondent Defenses"))
        for item in defenses[:8]:
            story.append(_bullet(item))
    if missing:
        story.append(_section_heading("Missing Proof / Evidence Gaps"))
        for item in missing[:8]:
            story.append(_bullet(item))

    evidence_files = case.get("evidence_files", [])
    if evidence_files:
        story.append(_section_heading("Evidence Files Uploaded"))
        for file_info in evidence_files[:12]:
            story.append(_bullet(f"{file_info.get('filename')} uploaded by {file_info.get('uploaded_by', 'unknown')} on {file_info.get('uploaded_at', 'unknown date')}"))


# ══════════════════════════════════════════════════════════════
# DOCUMENT 1 — DEMAND LETTER (Navy theme)
# ══════════════════════════════════════════════════════════════

DEMAND_LETTER_PROMPT = """
You are a senior Indian advocate drafting a formal legal demand letter.
Write exactly 6 paragraphs. Each paragraph must be 3-6 sentences.
Be formal, precise, and legally accurate.

Paragraph 1: FACTS — State the factual background clearly.
Paragraph 2: CLAIMANT POSITION — Explain why the claimant says liability exists.
Paragraph 3: RESPONDENT POSITION — Briefly summarize the likely defense position and why the claimant still disputes it.
Paragraph 4: LEGAL BASIS — Cite the applicable laws and sections.
Paragraph 5: DEMAND — State exactly what is demanded (amount/action), including important evidence and missing proof.
Paragraph 6: DEADLINE AND CLOSING — Give 15 days to comply, state consequences, and close formally.

Use formal legal English. Do not use bullet points.
Respond ONLY in JSON:
{
  "paragraph_1": "text",
  "paragraph_2": "text",
  "paragraph_3": "text",
  "paragraph_4": "text",
  "paragraph_5": "text",
  "paragraph_6": "text",
  "subject_line": "Re: Legal Notice — [dispute type]"
}
"""


async def _generate_demand_letter(
    case: dict,
    intake: dict,
    legal: dict,
    analytics: dict,
) -> bytes:
    """Generate demand letter PDF. Returns bytes."""

    # Get AI-written paragraphs
    context = f"""
Claimant: {case['claimant_name']}, {case['claimant_city']}, {case['claimant_state']}
Respondent: {case['respondent_name']}
Dispute: {case['dispute_text'][:800]}
Claim amount: Rs. {case.get('claim_amount', 'Not stated')}
Incident date: {case.get('incident_date', 'Not specified')}
Applicable laws: {[l.get('act') for l in legal.get('applicable_laws', [])[:4]]}
Legal standing: {legal.get('legal_standing')}
Key facts: {intake.get('key_facts', [])[:4]}
Claimant rights: {legal.get('claimant_rights', [])[:3]}
"""

    try:
        content = openai_service.call_json(
            system_prompt=DEMAND_LETTER_PROMPT,
            user_message=context,
            use_large_model=False,
            temperature=0.3,
            max_tokens=1500,
        )
    except Exception as llm_err:
        log_llm_fallback(logger, "Document _generate_demand_letter", llm_err)
        claim = case.get("claim_amount")
        laws = [
            f"{l.get('act')}" + (f", {l.get('section')}" if l.get("section") else "")
            for l in _as_list(legal.get("applicable_laws")) if isinstance(l, dict) and l.get("act")
        ][:3]
        forum = legal.get("forum_name") or "the competent court or forum"
        demand = (
            f"You are hereby called upon to pay Rs. {claim:,.0f} to my client within 15 days of receipt of this notice."
            if claim else
            "You are hereby called upon to stop the conduct described above and take the steps necessary to "
            "remedy it within 15 days of receipt of this notice."
        )
        content = {
            "subject_line": f"Re: Legal Notice — {intake.get('dispute_type') or 'Dispute'}",
            "paragraph_1":  f"This notice is issued on behalf of {case['claimant_name']} concerning the following: "
                            f"{case['dispute_text'][:600]}",
            "paragraph_2":  ("The conduct described gives rise to liability under "
                             + ("; ".join(laws) if laws else "applicable Indian law") + "."),
            "paragraph_3":  demand,
            "paragraph_4":  f"If you fail to comply within that period, my client will proceed before {forum} "
                            "without further notice, and will seek costs and interest as the court may allow.",
            "paragraph_5":  "You may respond, and propose a settlement, through the secure link sent to you with "
                            "this notice. All rights of my client are expressly reserved.",
        }

    # Build PDF
    buf   = io.BytesIO()
    doc   = _make_doc(buf)
    story = []

    story.extend(_header_block(
        NAVY,
        "LEGAL NOTICE",
        "Without Prejudice",
        case["id"],
    ))

    story.append(Spacer(1, 3*mm))
    story.append(_parties_table(case))
    story.append(Spacer(1, 4*mm))

    # Subject line
    story.append(Paragraph(
        _safe(content.get("subject_line", "Re: Legal Notice")),
        ParagraphStyle("SL", fontName="Helvetica-Bold", fontSize=10,
                       textColor=NAVY, spaceAfter=6),
    ))

    # Salutation
    story.append(_body_para(f"Dear {case['respondent_name']},"))
    story.append(Spacer(1, 2*mm))

    # 6 paragraphs
    for i in range(1, 7):
        para = content.get(f"paragraph_{i}", "")
        if para:
            story.append(_body_para(para))
            story.append(Spacer(1, 2*mm))

    # Closing
    story.append(Spacer(1, 4*mm))
    story.append(_body_para("Yours faithfully,"))
    story.append(Spacer(1, 8*mm))
    story.append(_body_para(f"{case['claimant_name']}"))
    story.append(_body_para(f"{case['claimant_city']}, {case['claimant_state']}"))
    story.append(_body_para(f"Date: {_today()}"))

    # Laws referenced
    laws = legal.get("applicable_laws", [])
    if laws:
        story.append(Spacer(1, 4*mm))
        story.append(_section_heading("Laws Referenced", NAVY))
        for law in laws[:6]:
            if not isinstance(law, dict):
                law = {"act": str(law)}
            story.append(_bullet(
                f"{law.get('act')} — Section {law.get('section')}: "
                f"{(law.get('relevance') or '')[:80]}"
            ))

    _append_case_record_sections(story, case, intake, legal, analytics, shared=True)  # sent to the respondent
    story.extend(_disclaimer_footer())
    doc.build(story)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════
# DOCUMENT 2 — COURT FILE (Red theme)
# ══════════════════════════════════════════════════════════════

COURT_FILE_PROMPT = """
You are a senior Indian court filing specialist.
Generate a structured litigation package.

Respond ONLY in JSON:
{
  "case_summary": "2-3 sentence case overview for court",
  "party_profile": {
    "claimant_background": "who the claimant is",
    "respondent_background": "who the respondent is",
    "relationship_context": "how the parties are connected"
  },
  "chronology": ["event 1", "event 2", "event 3"],
  "plaint_averments": ["averment 1", "averment 2", ...],
  "evidence_checklist": [
    {"item": "document name", "available": true|false, "importance": "CRITICAL|HIGH|MEDIUM"}
  ],
  "issues_in_dispute": ["issue 1", "issue 2"],
  "respondent_defenses": ["defense 1", "defense 2"],
  "witness_list": [
    {"role": "Claimant/Witness/Expert", "relevance": "what they will testify"}
  ],
  "pre_filing_actions": ["action 1", "action 2"],
  "filing_steps": ["step 1", "step 2"],
  "prayer_for_relief": ["relief 1", "relief 2"],
  "urgent_interim_relief": true|false,
  "interim_relief_grounds": "explanation or null"
}
"""


async def _generate_court_file(
    case: dict,
    intake: dict,
    legal: dict,
    analytics: dict,
) -> bytes:
    """Generate court-ready case file PDF."""

    context = f"""
Case: {case['dispute_text'][:600]}
Claimant: {case['claimant_name']}, {case['claimant_state']}
Respondent: {case['respondent_name']}
Track: {case.get('track')}
Claim: Rs. {(case.get('claim_amount') or 0):,.0f}
Legal standing: {legal.get('legal_standing')}
Forum: {legal.get('forum_name')}
Applicable laws: {[l.get('act') + ' S.' + str(l.get('section','')) for l in legal.get('applicable_laws',[])[:5]]}
Win probability: {analytics.get('win_probability')}%
Respondent defenses: {legal.get('respondent_defenses', [])}
Relief available: {legal.get('relief_available', [])}
"""

    try:
        content = openai_service.call_json(
            system_prompt=COURT_FILE_PROMPT,
            user_message=context,
            use_large_model=False,
            temperature=0.2,
            max_tokens=2000,
        )
    except Exception as llm_err:
        log_llm_fallback(logger, "Document _generate_court_file", llm_err)
        content = {
            "case_summary":         case["dispute_text"][:300],
            "plaint_averments":     ["Facts as stated in dispute description."],
            "evidence_checklist":   [{"item": "Dispute description", "available": True, "importance": "HIGH"}],
            "witness_list":         [{"role": "Claimant", "relevance": "Primary witness"}],
            "pre_filing_actions":   ["Collect all evidence", "Send legal notice"],
            "filing_steps":         ["File plaint at appropriate court"],
            "prayer_for_relief":    [f"Payment of Rs. {(case.get('claim_amount') or 0):,.0f}"],
            "urgent_interim_relief": False,
            "interim_relief_grounds": None,
        }

    buf   = io.BytesIO()
    doc   = _make_doc(buf)
    story = []

    story.extend(_header_block(
        RED,
        "COURT-READY CASE FILE",
        f"Prepared for: {legal.get('forum_name', 'Appropriate Court')}",
        case["id"],
    ))

    story.append(Spacer(1, 3*mm))
    story.append(_parties_table(case))
    story.append(Spacer(1, 4*mm))

    # Case Summary
    story.append(_section_heading("Case Summary", RED))
    story.append(_body_para(content.get("case_summary", "")))

    party_profile = content.get("party_profile", {})
    if party_profile:
        story.append(_section_heading("Party Profiles", RED))
        if party_profile.get("claimant_background"):
            story.append(_bullet(f"Claimant: {party_profile.get('claimant_background')}"))
        if party_profile.get("respondent_background"):
            story.append(_bullet(f"Respondent: {party_profile.get('respondent_background')}"))
        if party_profile.get("relationship_context"):
            story.append(_bullet(f"Relationship context: {party_profile.get('relationship_context')}"))

    chronology = _as_list(content.get("chronology"))
    if chronology:
        story.append(_section_heading("Chronology", RED))
        for i, item in enumerate(chronology[:10], 1):
            story.append(_body_para(f"{i}. {item}"))

    # Analytics snapshot
    story.append(_section_heading("Case Strength Assessment", RED))
    snap_data = [
        ["Win Probability", f"{analytics.get('win_probability', 'N/A')}%"],
        ["Legal Standing",  legal.get("legal_standing", "N/A")],
        ["Forum",           legal.get("forum_name", "N/A")],
        ["Court Cost Est.", f"Rs. {(analytics.get('court_cost_estimate') or 0):,.0f}"],
        ["Time Estimate",   f"{analytics.get('time_to_resolution_months', 'N/A')} months"],
    ]
    snap_tbl = Table(snap_data, colWidths=[60*mm, 110*mm])
    snap_tbl.setStyle(TableStyle([
        ("FONTNAME",      (0,0), (0,-1),  "Helvetica-Bold"),
        ("FONTSIZE",      (0,0), (-1,-1), 8.5),
        ("TEXTCOLOR",     (0,0), (0,-1),  MUTED),
        ("TEXTCOLOR",     (1,0), (1,-1),  DARK),
        ("GRID",          (0,0), (-1,-1), 0.3, colors.HexColor("#E2E8F0")),
        ("ROWBACKGROUNDS",(0,0), (-1,-1), [WHITE, LGREY]),
        ("TOPPADDING",    (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ("LEFTPADDING",   (0,0), (-1,-1), 6),
    ]))
    story.append(snap_tbl)
    story.append(Spacer(1, 3*mm))

    # Plaint averments
    story.append(_section_heading("Plaint Averments", RED))
    for i, av in enumerate(_as_list(content.get("plaint_averments")), 1):
        story.append(_body_para(f"{i}. {av}"))

    issues = _as_list(content.get("issues_in_dispute"))
    if issues:
        story.append(_section_heading("Issues In Dispute", RED))
        for item in issues[:10]:
            story.append(_bullet(item))

    defense_points = _as_list(content.get("respondent_defenses"))
    if defense_points:
        story.append(_section_heading("Respondent Defenses", RED))
        for item in defense_points[:10]:
            story.append(_bullet(item))

    # Evidence checklist
    story.append(_section_heading("Evidence Checklist", RED))
    ev_list = _as_list(content.get("evidence_checklist"))
    if ev_list:
        ev_data = [["Evidence Item", "Available", "Importance"]]
        for item in ev_list:
            if not isinstance(item, dict):
                item = {"item": str(item)}
            ev_data.append([
                xml_unescape(_safe(item.get("item", ""))),  # plain table cell, not markup
                "YES" if item.get("available") else "NO",
                item.get("importance", ""),
            ])
        ev_tbl = Table(ev_data, colWidths=[100*mm, 25*mm, 45*mm])
        ev_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (-1,0),   RED),
            ("TEXTCOLOR",     (0,0), (-1,0),   WHITE),
            ("FONTNAME",      (0,0), (-1,0),   "Helvetica-Bold"),
            ("FONTSIZE",      (0,0), (-1,-1),  8),
            ("GRID",          (0,0), (-1,-1),  0.3, colors.HexColor("#E2E8F0")),
            ("ROWBACKGROUNDS",(0,1), (-1,-1),  [WHITE, LGREY]),
            ("TOPPADDING",    (0,0), (-1,-1),  4),
            ("BOTTOMPADDING", (0,0), (-1,-1),  4),
            ("LEFTPADDING",   (0,0), (-1,-1),  5),
        ]))
        story.append(ev_tbl)

    # Witness list
    witnesses = _as_list(content.get("witness_list"))
    if witnesses:
        story.append(_section_heading("Witness List", RED))
        for w in witnesses:
            if not isinstance(w, dict):
                w = {"role": str(w)}
            story.append(_bullet(
                f"{w.get('role','')}: {w.get('relevance','')}"
            ))

    # Pre-filing actions
    actions = _as_list(content.get("pre_filing_actions"))
    if actions:
        story.append(_section_heading("Pre-Filing Actions Required", RED))
        for a in actions:
            story.append(_bullet(a))

    # Filing steps
    steps = _as_list(content.get("filing_steps"))
    if steps:
        story.append(_section_heading("Filing Steps", RED))
        for i, s in enumerate(steps, 1):
            story.append(_body_para(f"{i}. {s}"))

    # Prayer for relief
    prayers = _as_list(content.get("prayer_for_relief"))
    if prayers:
        story.append(_section_heading("Prayer for Relief", RED))
        for p in prayers:
            story.append(_bullet(p))

    # Applicable laws
    laws = legal.get("applicable_laws", [])
    if laws:
        story.append(_section_heading("Applicable Statutes", RED))
        for law in laws[:8]:
            if not isinstance(law, dict):
                law = {"act": str(law)}
            story.append(_bullet(
                f"{law.get('act')} — Section {law.get('section','N/A')} "
                f"[{law.get('strength','SUPPORTING')}]: {(law.get('relevance') or '')[:80]}"
            ))

    _append_case_record_sections(story, case, intake, legal, analytics)
    story.extend(_disclaimer_footer())
    doc.build(story)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════
# DOCUMENT 3 — SETTLEMENT AGREEMENT (Green theme)
# ══════════════════════════════════════════════════════════════

SETTLEMENT_PROMPT = """
You are a senior Indian advocate drafting a formal settlement agreement.
Generate exactly 11 numbered clauses.

CLAUSE STRUCTURE:
1. Recitals — background and context
2. Definitions — define key terms
3. Statement of Facts and Disputed Issues — summarize both positions respectfully
4. Settlement Amount/Actions — exact terms agreed
5. Payment Terms — how and when payment will be made (monetary cases)
6. Full and Final Release — claimant releases all claims
7. Confidentiality — neither party to disclose terms
8. Non-Disparagement — neither party to make negative statements
9. Representations and Warranties — each party confirms capacity to settle
10. Evidence and Record Integrity — what documents and communications informed the settlement
11. Breach, Governing Law and Dispute Resolution — governing law, jurisdiction, and default consequences

Respond ONLY in JSON:
{
  "recitals": "text",
  "clause_1_title": "Recitals",
  "clause_1_text": "text",
  "clause_2_title": "Definitions",
  "clause_2_text": "text",
  "clause_3_title": "Statement of Facts and Disputed Issues",
  "clause_3_text": "text",
  "clause_4_title": "Settlement Terms",
  "clause_4_text": "text",
  "clause_5_title": "Payment Terms",
  "clause_5_text": "text",
  "clause_6_title": "Full and Final Release",
  "clause_6_text": "text",
  "clause_7_title": "Confidentiality",
  "clause_7_text": "text",
  "clause_8_title": "Non-Disparagement",
  "clause_8_text": "text",
  "clause_9_title": "Representations and Warranties",
  "clause_9_text": "text",
  "clause_10_title": "Evidence and Record Integrity",
  "clause_10_text": "text",
  "clause_11_title": "Breach, Governing Law and Dispute Resolution",
  "clause_11_text": "text"
}
"""


def _fallback_settlement_clauses(case: dict, settled_amount: float, actions: list) -> dict:
    """
    Complete clauses for when no LLM is available. (The previous fallback
    printed "To be filled." in nine of eleven clauses, and "pay Rs. 0" for
    non-monetary settlements.)
    """
    claimant, respondent = case["claimant_name"], case["respondent_name"]
    city = case.get("claimant_city") or case.get("claimant_state") or "the Claimant's place of residence"
    if actions:
        terms = ("The Respondent shall carry out the following within 14 days of this Agreement: "
                 + "; ".join(f"({i}) {a}" for i, a in enumerate(actions, 1)) + ".")
        payment = "No payment is due under this Agreement; the obligations are the actions set out in Clause 4."
    else:
        terms = (f"The Respondent shall pay the Claimant Rs. {settled_amount:,.0f} "
                 f"(the \"Settlement Amount\") in full and final settlement of the dispute.")
        payment = ("The Settlement Amount shall be paid within 14 days of this Agreement by bank transfer "
                   "or UPI to an account nominated by the Claimant in writing. Payment is complete when "
                   "the funds are received by the Claimant.")
    clauses = [
        ("Recitals", f"A dispute arose between {claimant} and {respondent} concerning the matters described in "
                     "the Case Record above. The parties took part in AI-assisted mediation on the LegalAI "
                     "Resolver platform and have agreed to resolve the dispute on the terms below."),
        ("Definitions", "\"Agreement\" means this settlement agreement; \"Dispute\" means the matters described "
                        "in the Case Record; \"Effective Date\" means the date on which both parties accepted "
                        "these terms on the platform."),
        ("Statement of Facts and Disputed Issues",
         "Each party maintains its own account of the Dispute. This Agreement is a compromise and records "
         "that both parties prefer settlement to litigation."),
        ("Settlement Terms", terms),
        ("Payment Terms", payment),
        ("Full and Final Release",
         "On full performance of Clause 4, the Claimant releases the Respondent from all claims arising "
         "out of the Dispute, and neither party shall bring further proceedings concerning it."),
        ("No Admission of Liability",
         "This Agreement is a compromise. Nothing in it is an admission of liability or wrongdoing by either party."),
        ("Confidentiality and Non-Disparagement",
         "The parties shall keep the terms of this Agreement confidential, except as required by law or to "
         "enforce it, and shall not make disparaging public statements about each other concerning the Dispute."),
        ("Electronic Acceptance",
         "Both parties accepted these terms electronically on the platform. Contracts formed through "
         "electronic means are valid under Section 10A of the Information Technology Act, 2000."),
        ("Default",
         "If the Respondent fails to perform Clause 4 on time, the Claimant may enforce this Agreement, or "
         "pursue the original claim, before the competent court or forum, and may rely on this Agreement "
         "and the mediation record as evidence."),
        ("Governing Law and Jurisdiction",
         f"This Agreement is governed by the laws of India. The courts at {city} have jurisdiction over any "
         "dispute arising from it. This Agreement is the entire agreement between the parties on the Dispute."),
    ]
    content = {}
    for i, (title, text) in enumerate(clauses, 1):
        content[f"clause_{i}_title"] = title
        content[f"clause_{i}_text"] = text
    return content


async def _settlement_actions(case: dict) -> list:
    """Agreed actions for a non-monetary settlement (from the accepted round)."""
    if case.get("track") != "non_monetary":
        return []
    neg = cosmos_service.get_negotiation_by_case(case["id"])
    if not neg:
        return []
    rnd = next((r for r in neg.get("rounds", []) if r.get("round_number") == case.get("current_round", 1)), {})
    actions = rnd.get("ai_proposed_actions") or (rnd.get("respondent") or {}).get("actions") or []
    return [str(a) for a in _as_list(actions) if a]


async def _generate_settlement_pdf(
    case: dict,
    intake: dict,
    legal: dict,
    settled_amount: float,
    track: str,
) -> bytes:
    """Generate settlement agreement PDF."""
    actions = await _settlement_actions(case)

    context = f"""
Claimant: {case['claimant_name']}, {case['claimant_city']}, {case['claimant_state']}
Respondent: {case['respondent_name']}
Dispute type: {intake.get('dispute_category', case.get('track'))}
Original claim: Rs. {(case.get('claim_amount') or 0):,.0f}
Agreed settlement: {"; ".join(actions) if actions else f"Rs. {settled_amount:,.0f}"}
Governing state: {case['claimant_state']}
Settlement date: {_today()}
Track: {track}
Key issues resolved: {legal.get('key_legal_issues', [])}
"""

    try:
        content = openai_service.call_json(
            system_prompt=SETTLEMENT_PROMPT,
            user_message=context,
            use_large_model=True,   # gpt-4o — legal drafting needs quality
            temperature=0.2,
            max_tokens=3000,
        )
    except Exception as llm_err:
        log_llm_fallback(logger, "Document _generate_settlement_pdf", llm_err)
        content = _fallback_settlement_clauses(case, settled_amount, actions)

    buf   = io.BytesIO()
    doc   = _make_doc(buf)
    story = []

    story.extend(_header_block(
        GREEN,
        "SETTLEMENT AGREEMENT",
        "Full and Final Settlement — Legally Binding",
        case["id"],
    ))

    story.append(Spacer(1, 3*mm))

    # Settlement amount highlight box
    amount_data = [[
        Paragraph(
            ("AGREED SETTLEMENT: actions set out in Clause 4" if actions
             else f"AGREED SETTLEMENT: Rs. {settled_amount:,.0f}"),
            ParagraphStyle("SA", fontName="Helvetica-Bold", fontSize=14,
                           textColor=WHITE, alignment=TA_CENTER),
        )
    ]]
    amount_tbl = Table(amount_data, colWidths=[170*mm])
    amount_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), GREEN),
        ("TOPPADDING",    (0,0), (-1,-1), 10),
        ("BOTTOMPADDING", (0,0), (-1,-1), 10),
    ]))
    story.append(amount_tbl)
    story.append(Spacer(1, 4*mm))

    story.append(_parties_table(case))
    story.append(Spacer(1, 4*mm))

    # Preamble
    story.append(_body_para(
        f"This Settlement Agreement (\"Agreement\") is entered into on {_today()} "
        f"between {case['claimant_name']} (\"Claimant\") and "
        f"{case['respondent_name']} (\"Respondent\") through AI-assisted mediation "
        f"conducted by LegalAI Resolver."
    ))
    story.append(Spacer(1, 3*mm))

    _append_case_record_sections(story, case, intake, legal, shared=True)  # signed by both parties

    # 11 clauses
    for i in range(1, 12):
        title = content.get(f"clause_{i}_title", f"Clause {i}")
        text  = content.get(f"clause_{i}_text", "")
        if text:
            story.append(KeepTogether([
                _section_heading(f"{i}. {title}", GREEN),
                _body_para(text),
                Spacer(1, 2*mm),
            ]))

    # Signature blocks
    story.append(Spacer(1, 8*mm))
    story.append(_section_heading("Signatures", GREEN))
    story.append(Spacer(1, 3*mm))

    sig_data = [
        [
            Paragraph("CLAIMANT", ParagraphStyle("SL", fontName="Helvetica-Bold",
                                                  fontSize=8, textColor=MUTED)),
            Paragraph("RESPONDENT", ParagraphStyle("SR", fontName="Helvetica-Bold",
                                                    fontSize=8, textColor=MUTED)),
        ],
        [
            Paragraph("_" * 35, ParagraphStyle("SS", fontName="Helvetica", fontSize=10)),
            Paragraph("_" * 35, ParagraphStyle("SS", fontName="Helvetica", fontSize=10)),
        ],
        [
            Paragraph(_safe(case["claimant_name"]),
                      ParagraphStyle("SN", fontName="Helvetica-Bold", fontSize=9)),
            Paragraph(_safe(case["respondent_name"]),
                      ParagraphStyle("SN", fontName="Helvetica-Bold", fontSize=9)),
        ],
        [
            Paragraph(f"Date: {_today()}",
                      ParagraphStyle("SD", fontName="Helvetica", fontSize=8, textColor=MUTED)),
            Paragraph(f"Date: {_today()}",
                      ParagraphStyle("SD", fontName="Helvetica", fontSize=8, textColor=MUTED)),
        ],
    ]
    sig_tbl = Table(sig_data, colWidths=[85*mm, 85*mm])
    sig_tbl.setStyle(TableStyle([
        ("VALIGN",        (0,0), (-1,-1), "TOP"),
        ("TOPPADDING",    (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
        ("LEFTPADDING",   (0,0), (-1,-1), 4),
    ]))
    story.append(sig_tbl)

    story.extend(_disclaimer_footer())
    doc.build(story)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════
# DOCUMENT 4 — FIR ADVISORY (Orange theme)
# ══════════════════════════════════════════════════════════════

FIR_ADVISORY_PROMPT = """
You are a senior Indian criminal lawyer providing advisory for FIR filing.
For incidents on or after 1 July 2024 cite the Bharatiya Nyaya Sanhita (BNS) and
Bharatiya Nagarik Suraksha Sanhita (BNSS); for earlier incidents cite the IPC/CrPC.
Give the other code's equivalent in brackets, e.g. "BNS S.115(2) (earlier IPC 323)".
An FIR is registered under BNSS S.173; a Zero FIR can be filed at any police station.

Respond ONLY in JSON:
{
  "advisory_summary": "2 sentences explaining situation and recommendation",
  "ipc_sections_to_cite": [
    {"section": "BNS S.115(2) (earlier IPC 323)", "title": "Voluntarily causing hurt",
     "explanation": "Why this applies"}
  ],
  "fir_draft_points": [
    "Point 1 to mention in FIR",
    "Point 2 to mention in FIR"
  ],
  "filing_authority": "Which police station / authority",
  "filing_steps": ["step 1", "step 2"],
  "evidence_to_collect": ["evidence 1", "evidence 2"],
  "immediate_safety_steps": ["step 1 if danger"],
  "support_resources": [
    {"name": "Emergency (ERSS)", "contact": "112"},
    {"name": "Women Helpline", "contact": "181"}
  ],
  "mediation_note": "Why this case cannot be mediated"
}
"""


async def _generate_fir_advisory(
    case: dict,
    intake: dict,
    legal: dict,
) -> bytes:
    """Generate FIR advisory PDF for criminal track cases."""

    context = f"""
Incident: {case['dispute_text'][:600]}
Claimant: {case['claimant_name']}, {case['claimant_state']}
Respondent: {case['respondent_name']}
Criminal elements identified: {intake.get('criminal_elements', [])}
Immediate danger: {intake.get('immediate_danger', False)}
Criminal law sections (BNS / IPC): {legal.get('ipc_sections', [])}
Recommended authority: {intake.get('recommended_authority', 'local_police')}
"""

    try:
        content = openai_service.call_json(
            system_prompt=FIR_ADVISORY_PROMPT,
            user_message=context,
            use_large_model=False,
            temperature=0.2,
            max_tokens=2000,
        )
    except Exception as llm_err:
        log_llm_fallback(logger, "Document _generate_fir_advisory", llm_err)
        content = {
            "advisory_summary":    "This case involves criminal elements requiring police intervention.",
            "ipc_sections_to_cite": legal.get("ipc_sections", []),
            "fir_draft_points":    [case["dispute_text"][:200]],
            "filing_authority":    "Local Police Station",
            "filing_steps":        ["Visit local police station", "File written complaint"],
            "evidence_to_collect": ["All relevant documents", "Photographs if any"],
            "immediate_safety_steps": ["Contact police if in immediate danger"],
            "support_resources": [
                {"name": "Emergency (ERSS)", "contact": "112"},
                {"name": "Women Helpline", "contact": "181"},
            ],
            "mediation_note": "Criminal matters cannot be mediated.",
        }

    buf   = io.BytesIO()
    doc   = _make_doc(buf)
    story = []

    story.extend(_header_block(
        ORANGE,
        "CRIMINAL MATTER — LEGAL ADVISORY",
        "This document provides legal guidance only. Mediation is not applicable.",
        case["id"],
    ))

    # Warning box
    warn_data = [[Paragraph(
        "IMPORTANT: This matter involves criminal elements. "
        "If you are in immediate danger, call Police: 100 immediately.",
        ParagraphStyle("WP", fontName="Helvetica-Bold", fontSize=9,
                       textColor=WHITE, alignment=TA_CENTER),
    )]]
    warn_tbl = Table(warn_data, colWidths=[170*mm])
    warn_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), RED),
        ("TOPPADDING",    (0,0), (-1,-1), 8),
        ("BOTTOMPADDING", (0,0), (-1,-1), 8),
    ]))
    story.append(warn_tbl)
    story.append(Spacer(1, 4*mm))

    story.append(_parties_table(case))
    story.append(Spacer(1, 4*mm))

    story.append(_section_heading("Advisory Summary", ORANGE))
    story.append(_body_para(content.get("advisory_summary", "")))

    # IPC Sections
    ipc_sections = _as_list(content.get("ipc_sections_to_cite"))
    if ipc_sections:
        story.append(_section_heading("Applicable Criminal Law Sections (BNS / earlier IPC)", ORANGE))
        for sec in ipc_sections:
            if isinstance(sec, dict):
                story.append(_bullet(
                    f"{sec.get('section','')}: {sec.get('title','')} — "
                    f"{(sec.get('explanation') or '')[:100]}"
                ))
            else:
                story.append(_bullet(str(sec)))

    # FIR draft points
    fir_points = _as_list(content.get("fir_draft_points"))
    if fir_points:
        story.append(_section_heading("Key Points to Include in FIR", ORANGE))
        for i, pt in enumerate(fir_points, 1):
            story.append(_body_para(f"{i}. {pt}"))

    # Filing steps
    steps = _as_list(content.get("filing_steps"))
    if steps:
        story.append(_section_heading("Filing Steps", ORANGE))
        for i, s in enumerate(steps, 1):
            story.append(_body_para(f"{i}. {s}"))

    # Evidence to collect
    evidence = _as_list(content.get("evidence_to_collect"))
    if evidence:
        story.append(_section_heading("Evidence to Collect Before Filing", ORANGE))
        for ev in evidence:
            story.append(_bullet(ev))

    # Immediate safety steps
    safety = _as_list(content.get("immediate_safety_steps"))
    if safety:
        story.append(_section_heading("Immediate Safety Steps", RED))
        for s in safety:
            story.append(_bullet(s))

    # Support resources
    resources = _as_list(content.get("support_resources"))
    if resources:
        story.append(_section_heading("Support Resources", ORANGE))
        for r in resources:
            if not isinstance(r, dict):
                r = {"name": str(r)}
            if isinstance(r, dict):
                story.append(_bullet(f"{r.get('name','')}: {r.get('contact','')}"))

    story.append(Spacer(1, 4*mm))
    story.append(_body_para(
        content.get("mediation_note", "Criminal matters cannot be mediated.")
    ))

    story.extend(_disclaimer_footer())
    doc.build(story)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════
# DOCUMENT 5 — MEDIATION CERTIFICATE
# ══════════════════════════════════════════════════════════════

def _generate_mediation_certificate(case: dict) -> bytes:
    """
    One-page certificate of attempted mediation.
    Useful in court as evidence of good-faith pre-litigation attempt.
    No AI call needed — purely formatted from case data.
    """
    buf   = io.BytesIO()
    doc   = _make_doc(buf, margins={
        "left": 25*mm, "right": 25*mm,
        "top": 20*mm, "bottom": 20*mm,
    })
    story = []

    story.extend(_header_block(
        BLUE,
        "CERTIFICATE OF ATTEMPTED MEDIATION",
        "Pre-Litigation AI-Assisted Mediation",
        case["id"],
    ))

    story.append(Spacer(1, 6*mm))

    cert_text = (
        f"This is to certify that {case['claimant_name']} (Claimant) and "
        f"{case['respondent_name']} (Respondent) were involved in a dispute "
        f"regarding {case['dispute_text'][:150]}... and that a formal pre-litigation "
        f"mediation attempt was conducted through LegalAI Resolver, an AI-powered "
        f"dispute resolution platform, between {_fmt_date(case.get('created_at'))} and {_today()}."
    )
    story.append(_body_para(cert_text))
    story.append(Spacer(1, 4*mm))

    story.append(_body_para(
        "The mediation process involved AI-assisted analysis of applicable laws, "
        "evidence strength assessment, probabilistic case analytics, and structured "
        "negotiation rounds facilitated by the LegalAI Resolver platform."
    ))
    story.append(Spacer(1, 4*mm))

    details = [
        ["Case Reference",  case["id"][:8].upper()],
        ["Case Track",      (case.get("track") or "N/A").replace("_", " ").title()],
        ["Claim Amount",    f"Rs. {(case.get('claim_amount') or 0):,.0f}" if case.get("claim_amount") else "Non-monetary"],
        ["Claimant",        case["claimant_name"]],
        ["Respondent",      case["respondent_name"]],
        ["Date Initiated",  _fmt_date(case.get("created_at"))],
        ["Platform",        "LegalAI Resolver"],
    ]
    det_tbl = Table(details, colWidths=[60*mm, 100*mm])
    det_tbl.setStyle(TableStyle([
        ("FONTNAME",      (0,0), (0,-1),  "Helvetica-Bold"),
        ("FONTSIZE",      (0,0), (-1,-1), 9),
        ("TEXTCOLOR",     (0,0), (0,-1),  MUTED),
        ("TEXTCOLOR",     (1,0), (1,-1),  DARK),
        ("GRID",          (0,0), (-1,-1), 0.3, colors.HexColor("#E2E8F0")),
        ("ROWBACKGROUNDS",(0,0), (-1,-1), [WHITE, LGREY]),
        ("TOPPADDING",    (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ("LEFTPADDING",   (0,0), (-1,-1), 8),
    ]))
    story.append(det_tbl)

    story.append(Spacer(1, 6*mm))
    story.append(_body_para(
        "This certificate may be presented to any court or tribunal as evidence "
        "of a genuine pre-litigation attempt at dispute resolution as encouraged "
        "under the Legal Services Authorities Act 1987 and the Commercial Courts "
        "Act 2015."
    ))

    story.extend(_disclaimer_footer())
    doc.build(story)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════
# DOCUMENT 6 — BREACH OF SETTLEMENT NOTICE
# ══════════════════════════════════════════════════════════════

async def _generate_breach_notice_pdf(case: dict) -> bytes:
    """
    Generated when the claimant reports the settled amount was not paid.
    Stronger than demand letter — references the signed agreement.
    """
    buf   = io.BytesIO()
    doc   = _make_doc(buf)
    story = []

    dark_red = colors.HexColor("#7F1D1D")

    story.extend(_header_block(
        dark_red,
        "BREACH OF SETTLEMENT NOTICE",
        "Formal Notice of Non-Compliance with Executed Settlement Agreement",
        case["id"],
    ))

    story.append(Spacer(1, 3*mm))
    story.append(_parties_table(case))
    story.append(Spacer(1, 4*mm))

    story.append(_body_para(f"Dear {case['respondent_name']},"))
    story.append(Spacer(1, 2*mm))

    story.append(_body_para(
        f"This notice is issued pursuant to the Settlement Agreement executed "
        f"between the parties on {_fmt_date(case.get('settled_at'))} (Case Reference: "
        f"{case['id'][:8].upper()}) through LegalAI Resolver."
    ))

    story.append(_body_para(
        f"Under the terms of the executed Settlement Agreement, you agreed to pay "
        f"a sum of Rs. {(case.get('settled_amount') or 0):,.0f} to "
        f"{case['claimant_name']} (Claimant) within the stipulated payment period. "
        f"As of {_today()}, the said payment has not been received by the Claimant."
    ))

    story.append(_body_para(
        "Your failure to honor the executed Settlement Agreement constitutes a "
        "breach of contract under the Indian Contract Act 1872 and provides the "
        "Claimant with the right to seek enforcement through appropriate legal "
        "proceedings, including but not limited to filing a civil suit for recovery "
        "of the settled amount along with interest, costs, and damages."
    ))

    story.append(_body_para(
        f"You are hereby called upon to make payment of Rs. "
        f"{(case.get('settled_amount') or 0):,.0f} within 7 days of receipt of this "
        "notice. Failure to comply will leave the Claimant with no alternative but "
        "to initiate legal proceedings for enforcement of the Settlement Agreement, "
        "which will be submitted to the appropriate court as binding evidence of "
        "the agreed terms."
    ))

    story.append(Spacer(1, 4*mm))
    story.append(_body_para("Yours faithfully,"))
    story.append(Spacer(1, 8*mm))
    story.append(_body_para(case["claimant_name"]))
    story.append(_body_para(f"Date: {_today()}"))

    story.extend(_disclaimer_footer())
    doc.build(story)
    return buf.getvalue()
