"""WhatsApp/SMS channel layer: phone notices to respondents, opt-out, org settings, providers."""
from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.services import messaging
from app.services.messaging import normalize_phone
from tests.test_orgs import _org, _rows

PHONE = "98765 43210"
E164 = "919876543210"


@pytest.mark.parametrize("raw,expected", [
    ("9876543210", E164), ("+91 98765-43210", E164), ("09876543210", E164), ("919876543210", E164),
    ("0091 9876543210", E164), ("+44 7700 900123", "447700900123"), ("+1 (415) 555-0100", "14155550100"),
    ("12345", None), ("abc", None), ("", None), (None, None), ("5876543210", None),
])
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


def _phone_case(client, owner, org, n, i=0, phone=PHONE, auto_invite=True):
    row = {**_rows(n, 1, start=i)[0], "respondent_phone": phone}
    out = client.post(f"/api/orgs/{org['id']}/cases/bulk", headers=owner,
                      json={"rows": [row], "template": "loan_default", "auto_invite": auto_invite}).json()
    assert out["created"], out
    return out["created"][0]["case_id"]


def _phone_outbox(client):
    return client.get("/api/dev/outbox", params={"email": "+" + E164, "limit": 100}).json()["emails"]


def test_invite_goes_to_whatsapp_with_signed_link_and_is_logged(client, login):
    org, owner, n = _org(client, login)
    before = len(_phone_outbox(client))
    case_id = _phone_case(client, owner, org, n)

    msgs = _phone_outbox(client)
    assert len(msgs) == before + 1
    wa = msgs[0]
    assert wa["channel"] == "whatsapp" and "has raised a dispute with you" in wa["text"]
    assert f"/respond/{case_id}?t=" in wa["links"][0]

    case = client.get(f"/api/orgs/{org['id']}/cases/{case_id}", headers=owner).json()
    log = case["channel_log"]
    assert log[-1]["kind"] == "invite" and log[-1]["ok"] and log[-1]["channel"] == "whatsapp"
    assert log[-1]["to"] == "+91******10"          # masked: proof of service without exposing the number


def test_invalid_phone_rejected_in_bulk(client, login):
    org, owner, n = _org(client, login)
    row = {**_rows(n, 1)[0], "respondent_phone": "12-34"}
    out = client.post(f"/api/orgs/{org['id']}/cases/bulk", headers=owner,
                      json={"rows": [row], "template": "loan_default"}).json()
    assert not out["created"] and "respondent_phone" in out["errors"][0]["error"]


def test_respondent_can_opt_out_and_org_can_choose_channels(client, login):
    from urllib.parse import parse_qs, urlparse
    org, owner, n = _org(client, login)
    case_id = _phone_case(client, owner, org, n, i=1)
    token = parse_qs(urlparse(_phone_outbox(client)[0]["links"][0]).query)["t"][0]

    view = client.get(f"/api/respondent/case/{case_id}", params={"t": token}).json()
    assert view["phone_updates"] == {"has_phone": True, "number": "+91******10", "enabled": True}
    assert "channel_log" not in view["case"] and "respondent_phone" not in view["case"]

    r = client.post("/api/respondent/contact-preferences",
                    json={"case_id": case_id, "token": "wrong", "phone_updates": False})
    assert r.status_code == 403
    r = client.post("/api/respondent/contact-preferences",
                    json={"case_id": case_id, "token": token, "phone_updates": False})
    assert r.status_code == 200
    case = client.get(f"/api/orgs/{org['id']}/cases/{case_id}", headers=owner).json()
    assert case["respondent_channel_opt_out"] is True
    assert case["consent_log"][-1]["event"] == "phone_updates_off"
    assert messaging.messaging_service.notify_respondent(case, "test", "hello") is None

    # Organisation turns WhatsApp off: SMS is used instead; both off: nothing.
    case2 = _phone_case(client, owner, org, n, i=2, auto_invite=False)
    client.patch(f"/api/orgs/{org['id']}", headers=owner, json={"channels": {"whatsapp": False, "sms": True}})
    c2 = client.get(f"/api/orgs/{org['id']}/cases/{case2}", headers=owner).json()
    assert messaging.messaging_service.notify_respondent(c2, "test", "hello")["channel"] == "sms"
    client.patch(f"/api/orgs/{org['id']}", headers=owner, json={"channels": {"whatsapp": False, "sms": False}})
    assert messaging.messaging_service.notify_respondent(c2, "test", "hello") is None
    settings_out = client.get(f"/api/orgs/{org['id']}", headers=owner).json()["settings"]["channels"]
    assert settings_out == {"whatsapp": False, "sms": False}


class _FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        return self._body


def test_live_providers_whatsapp_template_then_sms_fallback(client, login, monkeypatch):
    org, owner, n = _org(client, login)
    case_id = _phone_case(client, owner, org, n, i=3, auto_invite=False)
    case = client.get(f"/api/orgs/{org['id']}/cases/{case_id}", headers=owner).json()

    for k, v in {"WHATSAPP_TOKEN": "wa-token", "WHATSAPP_PHONE_NUMBER_ID": "123",
                 "TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "tw", "TWILIO_FROM_NUMBER": "+15550001"}.items():
        monkeypatch.setattr(settings, k, v)
    calls, wa_status = [], {"code": 200}

    class FakeClient:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def post(self, url, **kw):
            calls.append((url, kw))
            if "graph.facebook.com" in url:
                return _FakeResponse(wa_status["code"], {"messages": [{"id": "wamid.1"}]} if wa_status["code"] == 200
                                     else {"error": {"message": "template not approved"}})
            return _FakeResponse(201, {"sid": "SM1"})
    monkeypatch.setattr(messaging.httpx, "Client", FakeClient)

    entry = messaging.messaging_service.notify_respondent(case, "invite", messaging.invite_text(case))
    assert entry["provider"] == "whatsapp_cloud" and entry["id"] == "wamid.1"
    url, kw = calls[-1]
    assert url.endswith("/123/messages") and kw["headers"]["Authorization"] == "Bearer wa-token"
    tpl = kw["json"]["template"]
    assert kw["json"]["to"] == E164 and tpl["name"] == "legalai_update"
    assert f"/respond/{case_id}?t=" in tpl["components"][0]["parameters"][1]["text"]

    wa_status["code"] = 400
    entry = messaging.messaging_service.notify_respondent(case, "proposal", "A proposal is ready.")
    assert entry["provider"] == "twilio_sms" and entry["ok"] and entry["id"] == "SM1"
    url, kw = calls[-1]
    assert "/Accounts/AC1/Messages.json" in url and kw["data"]["To"] == "+" + E164
    assert kw["data"]["Body"].startswith("A proposal is ready.") and "/respond/" in kw["data"]["Body"]
    assert len(kw["data"]["Body"]) <= messaging.SMS_MAX_CHARS

    log = client.get(f"/api/orgs/{org['id']}/cases/{case_id}", headers=owner).json()["channel_log"]
    assert [e["provider"] for e in log[-2:]] == ["whatsapp_cloud", "twilio_sms"]


def test_respondent_nudged_before_deadline_once_per_window(client, login):
    from app.core.expiry_worker import send_respondent_nudge
    from app.services.cosmos_service import cosmos_service
    org, owner, n = _org(client, login)
    case_id = _phone_case(client, owner, org, n, i=4)
    case = cosmos_service.get_case(case_id)
    assert case["status"] == "INVITE_SENT"
    deadline = datetime.fromisoformat(case["current_deadline"])
    resp = case["respondent_email"]

    assert send_respondent_nudge(case, now=deadline - timedelta(days=5)) is None
    assert send_respondent_nudge(case, now=deadline - timedelta(days=2)).endswith(":3d")
    case = cosmos_service.get_case(case_id)
    assert send_respondent_nudge(case, now=deadline - timedelta(days=2)) is None      # once per window
    assert send_respondent_nudge(case, now=deadline - timedelta(hours=10)).endswith(":1d")

    mails = [m for m in client.get("/api/dev/outbox", params={"email": resp}).json()["emails"]
             if m["subject"].startswith("Reminder: please respond")]
    assert len(mails) == 2
    nudges = [e for e in cosmos_service.get_case(case_id)["channel_log"] if e["kind"] == "nudge"]
    assert len(nudges) == 2


def test_auto_escalated_court_file_records_service_of_notice(client, login):
    import asyncio
    import io
    from pypdf import PdfReader
    from app.core.expiry_worker import _handle_expired_case
    from app.services.cosmos_service import cosmos_service
    org, owner, n = _org(client, login)
    case_id = _phone_case(client, owner, org, n, i=5)
    case = cosmos_service.get_case(case_id)
    assert [h["status"] for h in case["status_history"]][-1] == "INVITE_SENT"

    asyncio.run(_handle_expired_case(case))
    case = client.get(f"/api/orgs/{org['id']}/cases/{case_id}", headers=owner).json()
    assert case["status"] == "AUTO_ESCALATED"
    url = case["documents_data"]["court_file_url"]
    assert "court_file_escalated_" in url
    pdf = client.get(url.split("http://testserver", 1)[1]).content
    text = " ".join("\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages).split())
    assert "SERVICE OF NOTICE ON THE RESPONDENT" in text
    assert "Dispute notice emailed to" in text and "sent by WhatsApp to +91******10" in text
    assert "did not open the invite" in text
