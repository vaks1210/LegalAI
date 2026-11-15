import logging
from app.services.openai_service import openai_service

logger = logging.getLogger(__name__)


ROUTER_SYSTEM_PROMPT = """
You are a legal case classification expert for Indian law.
Classify the dispute into exactly ONE of the 5 tracks below.

TRACKS:
1. monetary_civil   - Disputes involving a specific money amount:
                      rent/security deposit, unpaid salary, consumer fraud,
                      contract breach, property damage, loan default,
                      cheque bounce, defamation with damages.

2. non_monetary     - Disputes with NO money involved:
                      apology demands, neighbor disputes, noise complaints,
                      boundary disputes, academic disputes,
                      social media defamation (apology only).

3. employment       - All employment disputes:
                      wrongful termination, unpaid notice period,
                      PF/gratuity non-payment, workplace harassment (civil/POSH),
                      experience letter withheld, non-compete violation.

4. consumer         - Consumer/service disputes:
                      e-commerce non-delivery, defective product,
                      insurance claim rejection, banking fraud,
                      telecom billing, real estate builder delay.

5. criminal         - Involves crime — NO mediation for this track:
                      physical assault, domestic violence,
                      sexual harassment (criminal), stalking,
                      cheating/fraud (IPC 420), cybercrime, threats.

CRIMINAL RED LINES — classify as criminal if ANY present:
- Physical violence occurred or is threatened
- Sexual assault or harassment
- Stalking or threats
- Domestic violence
- Criminal fraud / cheating (IPC 420)
- Cybercrime

Respond ONLY in this exact JSON. No text outside JSON.
{
  "track": "monetary_civil|non_monetary|employment|consumer|criminal",
  "confidence": 0.0 to 1.0,
  "is_criminal": true|false,
  "is_monetary": true|false,
  "respondent_is_company": true|false,
  "dispute_category": "specific e.g. rental_deposit|unpaid_salary|assault",
  "jurisdiction_state": "Indian state name or null",
  "estimated_claim_amount": number or null,
  "criminal_elements": ["list any criminal elements found or empty list"],
  "routing_notes": "one sentence explaining routing decision"
}
"""


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "y", "1")
    return False


def route_case(
    dispute_text: str,
    claim_amount: float | None,
    respondent_type: str,
    claimant_state: str,
) -> dict:
    """
    Fast pre-classification using gpt-4o-mini.
    Runs synchronously before the full pipeline.
    Result stored as case['routing'] in Cosmos.
    """
    user_msg = f"""
Dispute description:
{dispute_text}

Additional context:
- Claimant state: {claimant_state}
- Claim amount provided: {claim_amount if claim_amount else 'Not specified'}
- Respondent type: {respondent_type}

Classify this dispute into exactly one track.
"""

    try:
        result = openai_service.call_json(
            system_prompt=ROUTER_SYSTEM_PROMPT,
            user_message=user_msg,
            use_large_model=False,
            temperature=0.1,
            max_tokens=400,
        )

        # Ensure required fields exist
        required = [
            "track", "confidence", "is_criminal", "is_monetary",
            "respondent_is_company", "dispute_category",
            "criminal_elements", "routing_notes",
        ]
        for field in required:
            if field not in result:
                result[field] = None

        # LLMs sometimes answer booleans as strings ("no" is truthy in Python,
        # which wrongly forced civil disputes into the criminal track).
        for flag in ("is_criminal", "is_monetary", "respondent_is_company"):
            result[flag] = _as_bool(result.get(flag))
        elements = result.get("criminal_elements")
        if isinstance(elements, str):
            elements = [] if elements.strip().lower() in ("", "none", "no", "n/a", "[]") else [elements]
        result["criminal_elements"] = elements if isinstance(elements, list) else []

        # An LLM reply without a valid track (or with a bogus one) must not
        # leave the case untracked — that skips the criminal safety routing.
        valid_tracks = {"monetary_civil", "non_monetary", "employment", "consumer", "criminal"}
        if result.get("track") not in valid_tracks:
            logger.warning(f"Router LLM returned invalid track {result.get('track')!r} — using rule-based routing")
            from app.agents import offline_analysis
            return offline_analysis.route(dispute_text, claim_amount, respondent_type, claimant_state)

        # Safety override — if criminal elements found, force criminal track
        if result.get("is_criminal") and result.get("track") != "criminal":
            logger.warning(
                f"Criminal elements detected but track was '{result['track']}'. "
                f"Overriding to criminal."
            )
            result["track"] = "criminal"

        logger.info(
            f"Case routed | track={result['track']} | "
            f"confidence={result['confidence']} | "
            f"category={result['dispute_category']}"
        )
        return result

    except Exception as e:
        logger.warning(f"Case router LLM unavailable ({e}) — using rule-based routing")
        # Rule-based routing still detects criminal red flags, so violent or
        # criminal matters are never sent to mediation when the LLM is down.
        from app.agents import offline_analysis
        return offline_analysis.route(dispute_text, claim_amount, respondent_type, claimant_state)