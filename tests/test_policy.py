from datetime import timedelta
from decimal import Decimal

import pytest

from leash import policy
from leash.models import PurchaseRequest, Reason, Verdict
from leash.policy import SpendRecord
from leash.scenarios import EVIL, PIZZA, SUSHI
from tests.conftest import facts

REQ = PurchaseRequest(order_id="ORDER00001")


def ev(mandate, clock, order, ledger=(), req=REQ, **kw):
    return policy.evaluate(mandate, req, order, list(ledger), clock(), **kw)


def rec(clock, amount="10.00", ago_hours=0.0, payee=PIZZA, state="committed", order_id=None):
    return SpendRecord(request_id=f"r{amount}{ago_hours}{payee}", order_id=order_id or f"O{amount}{ago_hours}{payee}",
                       payee_ids=frozenset({payee}), amount=Decimal(amount), currency="USD",
                       at=clock() - timedelta(hours=ago_hours), state=state)


def test_allows_a_normal_purchase(mandate, clock):
    d = ev(mandate, clock, facts("20.00"))
    assert d.verdict == Verdict.ALLOW and d.reasons == [Reason.OK]


@pytest.mark.parametrize("order,reason", [
    (facts("61.00"), Reason.OVER_PER_PURCHASE_CAP),
    (facts("20.00", payee=("MIDX", EVIL)), Reason.MERCHANT_NOT_ALLOWED),
    (facts("20.00", currency="EUR"), Reason.CURRENCY_MISMATCH),
    (facts("20.00", status="COMPLETED"), Reason.ORDER_NOT_PAYABLE),
    (facts("20.00", status="VOIDED"), Reason.ORDER_NOT_PAYABLE),
    (facts("20.00", units=2), Reason.UNSUPPORTED_ORDER),
    (facts("20.00", units=0), Reason.UNSUPPORTED_ORDER),
    (facts(None), Reason.UNVERIFIABLE_ORDER),
    (facts("20.00", payee=()), Reason.UNVERIFIABLE_ORDER),
    (None, Reason.UNVERIFIABLE_ORDER),
])
def test_hard_blocks(mandate, clock, order, reason):
    d = ev(mandate, clock, order)
    assert d.verdict == Verdict.BLOCK and reason in d.reasons


def test_several_problems_are_all_reported(mandate, clock):
    d = ev(mandate, clock, facts("99.00", payee=("MIDX", EVIL), currency="EUR"))
    assert {Reason.OVER_PER_PURCHASE_CAP, Reason.MERCHANT_NOT_ALLOWED, Reason.CURRENCY_MISMATCH} <= set(d.reasons)


def test_cap_boundary(mandate, clock):
    assert ev(mandate, clock, facts("60.00"), human_approved=True).verdict == Verdict.ALLOW
    assert ev(mandate, clock, facts("60.01"), human_approved=True).verdict == Verdict.BLOCK


def test_step_up_boundary(mandate, clock):
    assert ev(mandate, clock, facts("39.99")).verdict == Verdict.ALLOW
    d = ev(mandate, clock, facts("40.00"))
    assert d.verdict == Verdict.ESCALATE and d.reasons == [Reason.NEEDS_OWNER_APPROVAL]


def test_owner_approval_unlocks_soft_flags_only(mandate, clock):
    d = ev(mandate, clock, facts("45.00"), human_approved=True)
    assert d.verdict == Verdict.ALLOW and d.approved_flags == [Reason.NEEDS_OWNER_APPROVAL]
    d = ev(mandate, clock, facts("45.00", payee=("MIDX", EVIL)), human_approved=True)
    assert d.verdict == Verdict.BLOCK  # a yes can never override a hard limit


def test_budget_boundary_and_rolling_window(mandate, clock):
    old = [rec(clock, "180.00", ago_hours=1, payee=SUSHI)]  # other merchant: keeps the split rule out of this test
    assert ev(mandate, clock, facts("20.00"), old).verdict == Verdict.ALLOW
    d = ev(mandate, clock, facts("20.01"), old)
    assert d.verdict == Verdict.BLOCK and Reason.OVER_PERIOD_BUDGET in d.reasons
    outside_window = [rec(clock, "180.00", ago_hours=24 * 7 + 1, payee=SUSHI)]
    assert ev(mandate, clock, facts("20.00"), outside_window).verdict == Verdict.ALLOW


def test_pending_reservations_count_towards_budget(mandate, clock):
    pending = [rec(clock, "190.00", ago_hours=1, payee=SUSHI, state="pending")]
    assert Reason.OVER_PERIOD_BUDGET in ev(mandate, clock, facts("20.00"), pending).reasons


def test_velocity_limit_and_24h_window(mandate, clock):
    two = [rec(clock, "5.00", 1, SUSHI), rec(clock, "6.00", 2, SUSHI)]
    assert ev(mandate, clock, facts("7.00"), two).verdict == Verdict.ALLOW
    three = two + [rec(clock, "8.00", 3, SUSHI)]
    assert Reason.TOO_MANY_PURCHASES in ev(mandate, clock, facts("7.00"), three).reasons
    yesterday = [rec(clock, "5.00", 25, SUSHI), rec(clock, "6.00", 26, SUSHI), rec(clock, "8.00", 27, SUSHI)]
    assert ev(mandate, clock, facts("7.00"), yesterday).verdict == Verdict.ALLOW


def test_split_purchase_detection(mandate, clock):
    earlier = [rec(clock, "14.00", 1), rec(clock, "15.00", 2)]
    d = ev(mandate, clock, facts("16.00"), earlier)
    assert d.verdict == Verdict.ESCALATE and Reason.SPLIT_PURCHASE_SUSPECTED in d.reasons
    other_merchant = [rec(clock, "14.00", 1, SUSHI), rec(clock, "15.00", 2, SUSHI)]
    assert ev(mandate, clock, facts("16.00"), other_merchant).verdict == Verdict.ALLOW


def test_possible_duplicate_window(mandate, clock):
    d = ev(mandate, clock, facts("10.00"), [rec(clock, "10.00", ago_hours=5 / 60)])
    assert d.verdict == Verdict.ESCALATE and Reason.POSSIBLE_DUPLICATE in d.reasons
    assert ev(mandate, clock, facts("10.00"), [rec(clock, "10.00", ago_hours=11 / 60)]).verdict == Verdict.ALLOW
    assert ev(mandate, clock, facts("11.00"), [rec(clock, "10.00", ago_hours=5 / 60)]).verdict == Verdict.ALLOW  # different amount


def test_same_order_cannot_be_paid_twice(mandate, clock):
    d = ev(mandate, clock, facts("20.00", order_id="ORDER00001"), [rec(clock, "20.00", 1, order_id="ORDER00001")])
    assert Reason.DUPLICATE_ORDER in d.reasons


def test_claims_must_match_paypal(mandate, clock):
    ok = PurchaseRequest(order_id="ORDER00001", claimed_amount="20.0", claimed_merchant=PIZZA.upper(), claimed_currency="usd")
    assert ev(mandate, clock, facts("20.00"), req=ok).verdict == Verdict.ALLOW  # case and trailing zeros do not matter
    for bad in (dict(claimed_amount="19.99"), dict(claimed_merchant=SUSHI), dict(claimed_currency="EUR")):
        d = ev(mandate, clock, facts("20.00"), req=PurchaseRequest(order_id="ORDER00001", **bad))
        assert d.verdict == Verdict.BLOCK and Reason.CLAIM_MISMATCH in d.reasons


def test_homoglyph_and_typosquat_merchants(mandate, clock):
    for sneaky in ("pizza-palace@exаmple.com",  # Cyrillic 'a'
                   "pizza-palace@examp1e.com", "pizza-paIace@example.com", "pizza-palace@example.co"):
        d = ev(mandate, clock, facts("20.00", payee=("MIDX", sneaky)))
        assert Reason.MERCHANT_NOT_ALLOWED in d.reasons and Reason.LOOKALIKE_MERCHANT in d.reasons, sneaky


def test_unrelated_merchant_is_not_called_a_lookalike(mandate, clock):
    d = ev(mandate, clock, facts("20.00", payee=("MIDX", "bookshop@other.org")))
    assert Reason.MERCHANT_NOT_ALLOWED in d.reasons and Reason.LOOKALIKE_MERCHANT not in d.reasons


@pytest.mark.parametrize("delta,reason", [(timedelta(days=-8), Reason.MANDATE_NOT_STARTED), (timedelta(days=8), Reason.MANDATE_EXPIRED)])
def test_mandate_time_window(mandate, clock, delta, reason):
    later = clock() + delta
    d = policy.evaluate(mandate, REQ, facts("20.00"), [], later)
    assert d.verdict == Verdict.BLOCK and d.reasons == [reason]


def test_kill_switch_beats_everything(mandate, clock):
    d = ev(mandate, clock, facts("20.00"), killed=True, human_approved=True)
    assert d.verdict == Verdict.BLOCK and d.reasons == [Reason.KILL_SWITCH]


def test_other_currency_records_are_ignored_for_budget(mandate, clock):
    eur = SpendRecord("r1", "O1", frozenset({PIZZA}), Decimal("190.00"), "EUR", clock(), "committed")
    assert ev(mandate, clock, facts("20.00"), [eur]).verdict == Verdict.ALLOW


def test_an_owner_approved_purchase_is_described_truthfully(mandate, clock):
    d = ev(mandate, clock, facts("45.00"), human_approved=True)
    assert d.verdict == Verdict.ALLOW and "Approved by the owner" in d.messages[0] and "45.00" in d.messages[0]
    assert "Within all" not in d.messages[0]
    assert ev(mandate, clock, facts("20.00")).messages == ["Within all of the owner's limits."]
