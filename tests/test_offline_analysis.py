from app.agents import offline_analysis as oa

DEPOSIT = (
    "I paid a security deposit of Rs. 60,000 under a registered leave and licence agreement. "
    "I vacated after written notice and have WhatsApp messages and bank transfer receipts. "
    "The landlord threatened to keep the deposit and stopped replying."
)


def test_deposit_dispute_is_classified_and_not_criminal():
    r = oa.route(DEPOSIT, None, "individual", "Maharashtra")
    assert r["track"] == "monetary_civil"
    assert r["dispute_category"] == "rental_deposit"
    assert r["is_criminal"] is False
    assert r["estimated_claim_amount"] == 60000


def test_violence_is_always_routed_to_criminal_track():
    text = "My neighbour attacked me with a rod over a parking dispute and threatened to kill me."
    r = oa.route(text, None, "individual", "Delhi")
    assert r["track"] == "criminal"
    assert r["is_criminal"] is True
    assert "physical violence" in r["criminal_elements"]


def test_amount_extraction_handles_lakh():
    assert oa.extract_amount("He owes me Rs 2.5 lakh since March") == 250000


def test_intake_and_legal_produce_useful_rule_based_output():
    case = {
        "dispute_text": DEPOSIT, "claimant_name": "A", "respondent_name": "B",
        "claimant_state": "Maharashtra", "claimant_city": "Pune", "claim_amount": 60000,
    }
    intake = oa.intake(case, "monetary_civil")
    assert intake["dispute_category"] == "rental_deposit"
    assert intake["evidence_strength_score"] >= 60
    legal = oa.legal(case, "monetary_civil", intake)
    acts = " ".join(l["act"] for l in legal["applicable_laws"])
    assert "Indian Contract Act" in acts
    assert "Maharashtra Rent Control Act" in acts
    assert legal["legal_standing"] in ("MODERATE", "STRONG")


def test_consumer_forum_follows_claim_amount():
    case = {"dispute_text": "x", "claim_amount": 30_000_000, "claimant_city": None}
    legal = oa.legal(case, "consumer", {"dispute_category": "consumer_general"})
    assert "National" in legal["forum_name"]


from app.agents.negotiation_agent import _fallback_proposal, _post_process

ANALYTICS = {"zopa_min": 31500, "zopa_max": 50400, "zopa_optimal": 42000}
CASE = {"track": "monetary_civil", "claim_amount": 60000}


def test_offline_proposal_stays_between_offers():
    for rnd, cl, rs in [(1, 55000, 35000), (2, 50000, 38000), (3, 46000, 42000), (1, 30000, 25000)]:
        amount = _fallback_proposal(CASE, rnd, cl, rs, ANALYTICS)["proposed_amount"]
        assert rs <= amount <= cl, (rnd, cl, rs, amount)


def test_offline_proposal_follows_party_movement():
    low = _fallback_proposal(CASE, 3, 40000, 34000, ANALYTICS)["proposed_amount"]
    high = _fallback_proposal(CASE, 3, 50000, 46000, ANALYTICS)["proposed_amount"]
    assert low < high


def test_llm_proposal_above_claimant_ask_is_clamped():
    result = _post_process(
        {"proposed_amount": 48000, "reasoning": "x" * 100}, CASE, ANALYTICS, 1,
        "monetary_civil", {}, claimant_offer=40000, respondent_offer=30000,
    )
    assert result["proposed_amount"] == 40000


def test_offline_non_monetary_proposes_actions():
    p = _fallback_proposal({"track": "non_monetary"}, 1,
                           ["Public apology", "Remove post"], ["Remove post"], {})
    assert "Remove post" in p["proposed_actions"]
    assert "Public apology" in p["proposed_actions"]
    assert "unavailable" not in p["reasoning"].lower()


def test_defamation_without_money_is_non_monetary():
    r = oa.route("My colleague posted false allegations about me online damaging my reputation. "
                 "I want the post removed and a public apology.", None, "individual", "Delhi")
    assert r["track"] == "non_monetary"
