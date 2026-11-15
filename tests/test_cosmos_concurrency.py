"""
The Cosmos branch of CosmosService._mutate (ETag + IfNotModified + retry)
can't run against a real account here, so exercise it with a fake
container that enforces ETags the way the SDK does.
"""
import copy
import threading
import uuid

import pytest
from azure.core import MatchConditions
from azure.cosmos import exceptions

from app.services.cosmos_service import CosmosService


class FakeCosmosContainer:
    def __init__(self):
        self.docs, self.lock, self.conflicts = {}, threading.Lock(), 0

    def create_item(self, body):
        with self.lock:
            doc = copy.deepcopy(body); doc["_etag"] = uuid.uuid4().hex
            self.docs[doc["id"]] = doc
            return copy.deepcopy(doc)

    def read_item(self, item, partition_key):
        with self.lock:
            if item not in self.docs:
                raise exceptions.CosmosResourceNotFoundError(message="not found")
            return copy.deepcopy(self.docs[item])

    def replace_item(self, item, body, etag=None, match_condition=None):
        with self.lock:
            current = self.docs[item]
            if match_condition == MatchConditions.IfNotModified and etag != current["_etag"]:
                self.conflicts += 1
                raise exceptions.CosmosAccessConditionFailedError(message="precondition failed")
            doc = copy.deepcopy(body); doc["_etag"] = uuid.uuid4().hex
            self.docs[item] = doc
            return copy.deepcopy(doc)


@pytest.fixture
def cosmos():
    svc = object.__new__(CosmosService)          # skip the real client setup
    svc.local = False
    svc.cases = FakeCosmosContainer()
    svc.negotiations = FakeCosmosContainer()
    return svc


def test_concurrent_writers_all_land_via_etag_retry(cosmos):
    cosmos.cases.create_item({"id": "c1", "n": 0})
    barrier = threading.Barrier(8)

    def bump():
        barrier.wait()
        for _ in range(10):
            cosmos._mutate(cosmos.cases, "c1", lambda d: d.__setitem__("n", d["n"] + 1), retries=200)

    threads = [threading.Thread(target=bump) for _ in range(8)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert cosmos.cases.read_item("c1", "c1")["n"] == 80
    assert cosmos.cases.conflicts > 0          # the retry path really ran


def test_both_party_offers_survive_on_cosmos(cosmos):
    cosmos.negotiations.create_item({"id": "n1", "case_id": "c1", "rounds": []})
    default = {"round_number": 1, "claimant": {}, "respondent": {}}
    barrier = threading.Barrier(2)

    def offer(party, amount):
        barrier.wait()
        cosmos.mutate_round("n1", 1, lambda r: r[party].update({"amount": amount}), default=default)

    ts = [threading.Thread(target=offer, args=a) for a in (("claimant", 50000), ("respondent", 30000))]
    [t.start() for t in ts]; [t.join() for t in ts]
    rounds = cosmos.negotiations.read_item("n1", "n1")["rounds"]
    assert len(rounds) == 1
    assert rounds[0]["claimant"]["amount"] == 50000 and rounds[0]["respondent"]["amount"] == 30000


def test_gives_up_cleanly_after_max_retries(cosmos):
    cosmos.cases.create_item({"id": "c2", "n": 0})
    always_conflict = FakeCosmosContainer()
    always_conflict.docs = cosmos.cases.docs

    def replace_conflict(item, body, etag=None, match_condition=None):
        raise exceptions.CosmosAccessConditionFailedError(message="precondition failed")
    always_conflict.replace_item = replace_conflict
    with pytest.raises(RuntimeError):
        cosmos._mutate(always_conflict, "c2", lambda d: None, retries=3)


def test_azure_blob_urls_are_re_signed_for_the_right_file(monkeypatch):
    from app.services.blob_service import BlobService
    svc = object.__new__(BlobService)
    svc.local, svc.client = False, None
    calls = []
    monkeypatch.setattr(svc, "generate_download_url",
                        lambda key, name, hours=72: calls.append((key, name)) or f"fresh://{key}/{name}")

    old = ("https://acct.blob.core.windows.net/generated-pdfs/demand_letter_abc.pdf"
           "?se=2026-01-01&sig=EXPIRED")
    assert svc.refresh_url(old) == "fresh://pdfs/demand_letter_abc.pdf"
    signed = "https://acct.blob.core.windows.net/signed-agreements/settlement_abc.pdf?sig=x"
    assert svc.refresh_url(signed) == "fresh://signed/settlement_abc.pdf"
    nested = "https://acct.blob.core.windows.net/evidence-uploads/case1/abc_photo%201.jpg?sig=x"
    assert svc.refresh_url(nested) == "fresh://evidence/case1/abc_photo 1.jpg"
    # Unknown containers / shapes are returned unchanged rather than broken.
    other = "https://example.com/some/file.pdf"
    assert svc.refresh_url(other) == other
    assert svc.refresh_url(None) is None
