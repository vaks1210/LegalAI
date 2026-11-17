"""
Two parties acting at the same moment must never erase each other's data.
Before atomic round updates, each request wrote back the whole round, so the
slower writer silently deleted the other party's offer or decision.
"""
import threading

from app.services.cosmos_service import CosmosService


def _service(tmp_path):
    return CosmosService(local_path=str(tmp_path / "race.db"))


def _run_concurrently(*fns, repeat=1):
    barrier = threading.Barrier(len(fns))
    errors = []

    def wrap(fn):
        def run():
            try:
                barrier.wait()
                for _ in range(repeat):
                    fn()
            except Exception as e:  # pragma: no cover - surfaced below
                errors.append(e)
        return run

    threads = [threading.Thread(target=wrap(f)) for f in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors


def test_simultaneous_offers_both_survive(tmp_path):
    svc = _service(tmp_path)
    for i in range(25):
        neg = svc.create_negotiation(f"case-{i}", {})
        default = {"round_number": 1, "claimant": {}, "respondent": {}}

        def offer(party, amount):
            return lambda: svc.mutate_round(
                neg["id"], 1,
                lambda r: r[party].update({"amount": amount, "submitted_at": "now"}),
                default=default,
            )

        _run_concurrently(offer("claimant", 50000), offer("respondent", 30000))
        rnd = svc.get_round(neg["id"], 1)
        assert rnd["claimant"]["amount"] == 50000
        assert rnd["respondent"]["amount"] == 30000
        assert len(svc.negotiations.read_item(neg["id"])["rounds"]) == 1


def test_concurrent_case_updates_are_not_lost(tmp_path):
    svc = _service(tmp_path)
    case = svc.create_case({"claimant_email": "a@x", "respondent_email": "b@x"})

    def bump(field):
        return lambda: svc._mutate_case(
            case["id"], lambda c: c.__setitem__(field, c.get(field, 0) + 1)
        )

    _run_concurrently(bump("a"), bump("b"), bump("a"), repeat=40)
    stored = svc.get_case(case["id"])
    assert stored["a"] == 80
    assert stored["b"] == 40
