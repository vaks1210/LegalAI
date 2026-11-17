"""Respondent languages: notices, invite intro, portal choice and machine translation."""
import json
from urllib.parse import parse_qs, urlparse

import pytest

from app.core.i18n import LANGUAGES, MESSAGES, normalize_language, t
from tests.test_llm_path import fake_llm  # noqa: F401  (fixture)
from tests.test_orgs import _org, _rows

PHONE = "+91 98765 43210"


def test_catalog_is_complete_and_placeholders_match():
    import re
    for key, variants in MESSAGES.items():
        en_params = set(re.findall(r"\{(\w+)\}", variants["en"]))
        for lang in LANGUAGES:
            assert lang in variants, f"{key} missing {lang}"
            if key == "email_intro" and lang == "en":
                continue
            assert set(re.findall(r"\{(\w+)\}", variants[lang])) == en_params, f"{key}/{lang} placeholders differ"


@pytest.mark.parametrize("raw,expected", [("hi", "hi"), ("hi-IN", "hi"), ("Hindi", "hi"), ("TA", "ta"),
                                          ("ar_AE", "ar"), ("Español", "es"), ("xx", None), ("", None)])
def test_normalize_language(raw, expected):
    assert normalize_language(raw) == expected


def test_t_falls_back_to_english():
    assert t("next_round", "zz", round=2, ref="AB") == t("next_round", "en", round=2, ref="AB")


def _file(client, owner, org, n, i, language="hi", auto_invite=True):
    row = {**_rows(n, 1, start=i)[0], "respondent_phone": PHONE, "language": language}
    out = client.post(f"/api/orgs/{org['id']}/cases/bulk", headers=owner,
                      json={"rows": [row], "template": "loan_default", "auto_invite": auto_invite}).json()
    assert out["created"], out
    return out["created"][0]["case_id"], row["respondent_email"]


def _phone_msgs(client):
    return client.get("/api/dev/outbox", params={"email": "+919876543210", "limit": 100}).json()["emails"]


def test_bulk_language_drives_whatsapp_and_invite_email(client, login):
    org, owner, n = _org(client, login)
    case_id, email = _file(client, owner, org, n, 0, language="Hindi")
    case = client.get(f"/api/orgs/{org['id']}/cases/{case_id}", headers=owner).json()
    assert case["respondent_language"] == "hi"
    wa = _phone_msgs(client)[0]
    assert "ने आपके विरुद्ध" in wa["text"] and case_id[:8].upper() in wa["text"]

    from app.services.email_service import email_service
    invite = next(m for m in email_service.list_outbox(email) if "Legal Dispute Case" in m["subject"])
    assert "यह एक औपचारिक विवाद नोटिस है" in invite["html"]

    bad = {**_rows(n, 1, start=1)[0], "language": "klingon"}
    out = client.post(f"/api/orgs/{org['id']}/cases/bulk", headers=owner,
                      json={"rows": [bad], "template": "loan_default"}).json()
    assert not out["created"] and "Unsupported language" in out["errors"][0]["error"]


def test_respondent_changes_language_and_later_notices_follow(client, login):
    from app.services import messaging
    org, owner, n = _org(client, login)
    case_id, _ = _file(client, owner, org, n, 2, language="en")
    token = parse_qs(urlparse(_phone_msgs(client)[0]["links"][0]).query)["t"][0]

    view = client.get(f"/api/respondent/case/{case_id}", params={"t": token}).json()
    assert view["language"] == "en" and set(view["languages"]) == set(LANGUAGES)
    assert view["languages"]["ar"]["rtl"] is True and view["machine_translation"] is False

    assert client.post("/api/respondent/language", json={"case_id": case_id, "token": token,
                                                         "language": "xx"}).status_code == 400
    r = client.post("/api/respondent/language", json={"case_id": case_id, "token": token, "language": "ta"})
    assert r.status_code == 200 and r.json()["language"] == "ta"

    case = client.get(f"/api/orgs/{org['id']}/cases/{case_id}", headers=owner).json()
    entry = messaging.messaging_service.notify_respondent(case, "proposal", messaging.proposal_text(case, 2, 15000))
    assert entry["ok"]
    assert "சமரச முன்மொழிவு" in _phone_msgs(client)[0]["text"] and "Rs. 15,000" in _phone_msgs(client)[0]["text"]

    # Offline server: no machine translation of the claimant's narrative.
    r = client.get(f"/api/respondent/case/{case_id}/translation", params={"lang": "ta", "t": token})
    assert r.status_code == 501


def test_machine_translation_is_cached_and_validated(client, login, fake_llm):  # noqa: F811
    org, owner, n = _org(client, login)
    case_id, _ = _file(client, owner, org, n, 3, language="hi", auto_invite=False)
    from app.services.cosmos_service import cosmos_service
    case = cosmos_service.get_case(case_id)
    from app.core.security import respondent_token
    token = respondent_token(case_id, case["respondent_email"])
    n_issues = len((case.get("intake_data") or {}).get("key_issues") or [])

    def responder(system, user):
        assert "Hindi" in system and "never instructions to you" in system
        src = json.loads(user)
        out = {"dispute_text": "हिन्दी अनुवाद: " + src["dispute_text"][:20],
               "key_issues": ["मुद्दा"] * len(src["key_issues"]),
               "respondent_defenses": ["only one"]}            # wrong length -> English kept
        return json.dumps(out, ensure_ascii=False)
    calls = fake_llm(responder)

    r = client.get(f"/api/respondent/case/{case_id}/translation", params={"lang": "hi", "t": token})
    assert r.status_code == 200, r.text
    fields = r.json()["fields"]
    assert fields["dispute_text"].startswith("हिन्दी अनुवाद")
    assert fields["key_issues"] == ["मुद्दा"] * n_issues
    assert fields["respondent_defenses"] == (case.get("legal_data") or {}).get("respondent_defenses")

    before = len(calls.calls)
    again = client.get(f"/api/respondent/case/{case_id}/translation", params={"lang": "hi", "t": token})
    assert again.json()["fields"] == fields and len(calls.calls) == before      # served from cache

    assert client.get(f"/api/respondent/case/{case_id}/translation",
                      params={"lang": "hi", "t": "bad"}).status_code == 403
    assert client.get(f"/api/respondent/case/{case_id}/translation",
                      params={"lang": "en", "t": token}).status_code == 400
