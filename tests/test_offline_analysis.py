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


def test_criminal_advisory_cites_current_bns_sections():
    text = "My neighbour attacked me with a rod and threatened to kill me if I complain."
    case = {"dispute_text": text, "claimant_state": "Delhi", "claimant_city": "Delhi",
            "routing": oa.route(text, None, "individual", "Delhi")}
    legal = oa.legal(case, "criminal", oa.intake(case, "criminal"))
    joined = " ".join(legal["ipc_sections"])
    assert "BNS S.115(2)" in joined and "BNS S.351" in joined
    assert "earlier IPC 323" in joined


def test_prompts_do_not_cite_struck_down_or_repealed_provisions_as_current():
    import app.agents.legal_agent as la
    text = la.CRIMINAL_PROMPT
    assert "CrPC S.154 —" not in text and "Police Emergency: 100" not in text
    assert "BNSS S.173" in text
    # S.66A IT Act was struck down in 2015 and must only appear as a warning.
    for prompt in (getattr(la, n) for n in dir(la) if n.endswith("_PROMPT")):
        if "66A" in prompt:
            assert "struck down" in prompt


def test_employment_cites_labour_codes_with_earlier_acts():
    case = {"dispute_text": "My employer has not paid my salary for two months.", "claim_amount": 80000,
            "claimant_state": "Karnataka", "claimant_city": "Bengaluru"}
    intake = oa.intake(case, "employment")
    legal = oa.legal(case, "employment", intake)
    acts = " ".join(l["act"] + " " + l["section"] for l in legal["applicable_laws"])
    assert "Code on Wages, 2019" in acts
    assert "earlier Payment of Wages Act" in acts


def test_shared_reasoning_never_reveals_zopa():
    analytics = {"zopa_min": 31500, "zopa_max": 50400, "zopa_optimal": 42000}
    for rnd in (1, 2, 3):
        text = _fallback_proposal(CASE, rnd, 55000, 35000, analytics)["reasoning"]
        for secret in ("42,000", "31,500", "50,400", "anchor", "%"):
            assert secret not in text, (rnd, secret, text)


def test_llm_reasoning_that_leaks_zopa_is_replaced():
    leaky = ("Our internal range runs from Rs. 31,500 to Rs. 50,400, so we propose Rs. 40,000 " * 2)
    out = _post_process({"proposed_amount": 40000, "reasoning": leaky}, CASE, ANALYTICS, 1,
                        "monetary_civil", {}, claimant_offer=55000, respondent_offer=30000)
    assert "31,500" not in out["reasoning"] and "50,400" not in out["reasoning"]

    fine = ("We propose Rs. 42,000 because the deposit was documented and court would take years. " * 2)
    out = _post_process({"proposed_amount": 42000, "reasoning": fine}, CASE, ANALYTICS, 1,
                        "monetary_civil", {}, claimant_offer=55000, respondent_offer=30000)
    assert out["reasoning"] == fine  # the proposal itself may equal the optimum
