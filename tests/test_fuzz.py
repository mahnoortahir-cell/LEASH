"""Randomised stress test: whatever a misbehaving agent does, the money, the books and the rules agree."""
import random
from datetime import timedelta

import pytest

from leash.guard import GuardError
from leash.models import PurchaseRequest, Status
from leash.scenarios import EVIL, PIZZA, SUSHI, make_harness
from leash.util import parse_ts
from leash.clock import FakeClock
from decimal import Decimal

MERCHANTS = [PIZZA, SUSHI, EVIL, "pizza-palace@examp1e.com", "sushi-go@example.com", "x@y.z"]


def check_invariants(h, m):
    captures = h.mock.captured
    allowed = set(m.allowed_merchants)
    timed = []
    for c in captures:
        order = h.mock._orders[c.order_id]
        assert order["payee"].lower() in allowed, "money went to a merchant that is not allowed"
        assert c.currency == m.currency and c.amount <= m.per_purchase_cap
        when = parse_ts(c.created_time)
        assert m.valid_from <= when < m.valid_until, "money moved outside the mandate's dates"
        timed.append((when, c.amount))
    for when, _ in timed:
        week = sum(a for u, a in timed if when - timedelta(hours=m.budget_window_hours) < u <= when)
        day = sum(1 for u, _ in timed if when - timedelta(hours=24) < u <= when)
        assert week <= m.budget_amount, "budget exceeded"
        assert day <= m.max_purchases_per_day, "too many purchases in a day"
    shown, authorised, needed_ok = {}, {}, set()
    for ev in h.audit.events():
        rid = ev.data.get("request_id")
        if ev.type == "order_verified":
            shown.setdefault(rid, ev.data["order"]["amount"])
        elif ev.type == "approval_requested":
            needed_ok.add(rid)
        elif ev.type == "payment_authorized":
            authorised[rid] = ev.data["order"]["amount"]
        elif ev.type == "order_captured" and ev.data.get("note") != "captured by the merchant":
            assert Decimal(ev.data["amount"]) == Decimal(authorised[rid]), "charged a different amount than was authorised"
            if rid in needed_ok:
                assert Decimal(ev.data["amount"]) == Decimal(shown[rid]), "the owner approved one amount but another was charged"
    assert h.audit.verify().ok, "audit chain broken"
    now = h.clock()
    real = sum((a for u, a in timed if now - timedelta(hours=m.budget_window_hours) < u <= now), Decimal("0"))
    assert h.guard.status()["spent_in_window"] == real, "the books disagree with the money that really moved"


@pytest.mark.parametrize("seed", range(40))
def test_random_agent_behaviour_never_breaks_the_invariants(seed, tmp_path):
    rng = random.Random(seed)
    clock = FakeClock()
    path = tmp_path / "audit.jsonl"
    h = make_harness(clock=clock, log_path=path, fsync=False, max_purchases_per_day=1 + seed % 3)  # tight limits get hit often
    mandate = h.guard.mandate
    pending, seen = [], []
    actions = ["pay", "advance", "approve", "reject", "kill", "release", "garbage", "replay", "restart", "fail", "amend", "switch_then_approve"]
    weights = [40, 15, 10, 4, 3, 3, 5, 5, 3, 3, 5, 8]
    orders, waiting_orders = [], {}
    for _ in range(80):
        action = rng.choices(actions, weights)[0]
        if action == "pay":
            amount = str(rng.choice([5, 12.5, 25, 38, 39.99, 40, 45, 59.99, 60, 61, 100]))
            oid = h.order(rng.choice(MERCHANTS), amount, currency=rng.choice(["USD"] * 8 + ["EUR"]))
            orders.append(oid)
            claims = {}
            if rng.random() < 0.3:
                claims["claimed_merchant"] = rng.choice(MERCHANTS)
            if rng.random() < 0.2:
                claims["claimed_amount"] = Decimal(rng.choice(["5", "38", "45"]))
            req = PurchaseRequest(order_id=oid, **claims)
            seen.append(req)
            result = h.guard.pay(req)
            if result.status == Status.NEEDS_OWNER_APPROVAL:
                pending.append(result.request_id)
                waiting_orders[result.request_id] = oid
        elif action == "advance":
            clock.advance(minutes=rng.choice([1, 5, 30, 600, 1500, 3000]))
        elif action in ("approve", "reject") and pending:
            rid = pending.pop(rng.randrange(len(pending)))
            waiting_orders.pop(rid, None)
            try:
                (h.guard.approve if action == "approve" else h.guard.reject)(rid)
            except GuardError:
                pass
        elif action == "kill":
            h.guard.engage_kill_switch()
        elif action == "release":
            h.guard.release_kill_switch()
        elif action == "garbage":
            h.guard.pay(rng.choice([{}, {"order_id": "../x"}, {"order_id": "ORDGOOD123", "claimed_amount": "-1"}]))
        elif action == "replay" and seen:
            h.guard.pay(rng.choice(seen))
        elif action == "restart":
            fresh = make_harness(log_path=path, clock=clock, mock=h.mock, with_mandate=False, fsync=False)
            h.guard, h.audit = fresh.guard, fresh.audit
        elif action == "fail":
            h.mock.fail_next("capture_order")
        elif action == "switch_then_approve" and pending:  # the exact trick: the price changes, then the owner says yes
            rid = pending.pop(rng.randrange(len(pending)))
            h.mock.amend_order_amount(waiting_orders.pop(rid), Decimal(rng.choice(["5", "38", "45", "59.99", "99"])))
            try:
                h.guard.approve(rid)
            except GuardError:
                pass
        elif action == "amend" and orders:  # a merchant changes the price of an order at a random moment
            target = rng.choice(list(waiting_orders.values())) if (waiting_orders and rng.random() < 0.8) else rng.choice(orders)
            h.mock.amend_order_amount(target, Decimal(rng.choice(["5", "38", "45", "59.99", "99"])))
        check_invariants(h, mandate)
