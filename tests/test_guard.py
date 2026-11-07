from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from leash.audit import AuditLog
from leash.guard import Guard, GuardError
from leash.mock_paypal import MockPayPal
from leash.models import PurchaseRequest, Reason, Status
from leash.scenarios import PIZZA, SUSHI, demo_mandate, make_harness


def event_types(h):
    return [e.type for e in h.audit.events()]


def test_no_mandate_means_no_payments_and_no_paypal_calls():
    h = make_harness(with_mandate=False)
    r = h.pay(h.order(PIZZA, "20.00"))
    assert r.status == Status.BLOCKED and Reason.NO_MANDATE in r.decision.reasons
    assert "get_order" not in h.mock.calls and h.mock.captured_total == 0


def test_malformed_requests_are_logged_and_blocked():
    h = make_harness()
    r = h.guard.pay({"order_id": "x"})
    assert r.status == Status.BLOCKED and Reason.INVALID_REQUEST in r.decision.reasons
    assert "request_invalid" in event_types(h) and h.audit.verify().ok


def test_owner_calls_with_unknown_ids_raise():
    h = make_harness()
    for call in (lambda: h.guard.approve("nope12345"), lambda: h.guard.reject("nope12345"), lambda: h.guard.finalize("nope12345")):
        with pytest.raises(GuardError):
            call()


def test_a_yes_cannot_override_a_hard_limit_that_appeared_meanwhile(clock):
    h = make_harness(clock=clock)
    waiting = h.pay(h.order(SUSHI, "45.00"))
    assert waiting.status == Status.NEEDS_OWNER_APPROVAL
    for _ in range(5):                      # meanwhile the budget gets used up
        assert h.pay(h.order(PIZZA, "38.00")).status == Status.COMPLETED
        clock.advance(hours=25)
    r = h.guard.approve(waiting.request_id)
    assert r.status == Status.BLOCKED and Reason.OVER_PERIOD_BUDGET in r.decision.reasons
    assert h.mock.captured_total == Decimal("190.00")


def test_kill_switch_pressed_after_authorisation_stops_the_capture():
    h = make_harness(auto_checkout=False)
    oid = h.order(SUSHI, "30.00")
    first = h.pay(oid)
    h.guard.engage_kill_switch("panic")
    h.mock.simulate_checkout_approval(oid)
    r = h.guard.finalize(first.request_id)
    assert r.status == Status.BLOCKED and Reason.KILL_SWITCH in r.decision.reasons
    assert h.mock.captured_total == 0 and h.guard.status()["spent_in_window"] == 0


def test_unapproved_orders_expire_and_give_the_budget_back(clock):
    h = make_harness(auto_checkout=False, clock=clock)
    assert h.pay(h.order(PIZZA, "38.00")).status == Status.AWAITING_CHECKOUT
    assert h.guard.status()["spent_in_window"] == Decimal("38.00")
    clock.advance(hours=7)
    assert h.guard.status()["spent_in_window"] == 0


def test_awaiting_then_finalize_flow():
    h = make_harness(auto_checkout=False)
    oid = h.order(PIZZA, "20.00")
    first = h.pay(oid)
    assert first.status == Status.AWAITING_CHECKOUT and first.approval_url and "checkoutnow" in first.approval_url
    assert h.guard.finalize(first.request_id).status == Status.AWAITING_CHECKOUT  # buyer has not clicked yet
    h.mock.simulate_checkout_approval(oid)
    done = h.guard.finalize(first.request_id)
    assert done.status == Status.COMPLETED and done.capture_id and h.mock.captured_total == Decimal("20.00")


def test_merchant_capturing_first_is_recorded_truthfully():
    h = make_harness(auto_checkout=False)
    oid = h.order(SUSHI, "30.00")
    first = h.pay(oid)
    h.mock.simulate_checkout_approval(oid)
    h.mock.capture_order(oid, request_id="merchant")     # the merchant captures before Leash finalises
    done = h.guard.finalize(first.request_id)
    assert done.status == Status.COMPLETED and done.capture_id
    assert h.guard.status()["spent_in_window"] == Decimal("30.00") == h.mock.captured_total


def test_lookup_failure_blocks_and_is_logged():
    h = make_harness()
    oid = h.order(PIZZA, "20.00")
    h.mock.fail_next("get_order", issue="INTERNAL_SERVER_ERROR", status=500, name="INTERNAL_SERVER_ERROR")
    r = h.pay(oid)
    assert r.status == Status.BLOCKED and Reason.UNVERIFIABLE_ORDER in r.decision.reasons
    assert "order_lookup_failed" in event_types(h) and h.mock.captured_total == 0


def test_capture_failure_is_logged_and_releases_budget():
    h = make_harness()
    h.mock.fail_next("capture_order")
    r = h.pay(h.order(PIZZA, "38.00"))
    assert r.status == Status.FAILED and {"order_capture_failed", "order_released"} <= set(event_types(h))


def test_duplicate_request_is_logged_not_recharged():
    h = make_harness()
    req = PurchaseRequest(order_id=h.order(PIZZA, "20.00"))
    h.guard.pay(req)
    h.guard.pay(req)
    assert event_types(h).count("duplicate_request_ignored") == 1 and len(h.mock.captured) == 1


def test_pending_approvals_survive_a_restart(tmp_path):
    path = tmp_path / "audit.jsonl"
    h = make_harness(log_path=path)
    waiting = h.pay(h.order(SUSHI, "45.00"))
    fresh = make_harness(log_path=path, clock=h.clock, mock=h.mock, with_mandate=False)
    assert fresh.guard.status()["waiting_for_owner"] == [waiting.request_id]
    assert fresh.guard.approve(waiting.request_id, approver="Sam").status == Status.COMPLETED


def test_kill_switch_survives_a_restart(tmp_path):
    path = tmp_path / "audit.jsonl"
    h = make_harness(log_path=path)
    h.guard.engage_kill_switch("stop")
    fresh = make_harness(log_path=path, clock=h.clock, mock=h.mock, with_mandate=False)
    assert fresh.guard.killed
    assert fresh.pay(fresh.order(PIZZA, "20.00")).status == Status.BLOCKED


def test_same_mandate_is_not_logged_twice_on_restart(tmp_path, clock):
    path = tmp_path / "audit.jsonl"
    m = demo_mandate(clock)
    mock = MockPayPal(clock)
    Guard(mock, AuditLog(path, clock=clock), mandate=m, clock=clock)
    Guard(mock, AuditLog(path, clock=clock), mandate=m, clock=clock)  # a "restart" with the same rules
    assert [e.type for e in AuditLog(path).events()].count("mandate_set") == 1


def test_changing_the_mandate_is_logged_and_takes_effect(clock):
    h = make_harness(clock=clock)
    h.guard.set_mandate(demo_mandate(clock, per_purchase_cap=Decimal("30"), step_up_threshold=Decimal("20")), by="Sam")
    assert h.pay(h.order(PIZZA, "25.00")).status == Status.NEEDS_OWNER_APPROVAL
    r = h.pay(h.order(PIZZA, "35.00"))
    assert r.status == Status.BLOCKED and Reason.OVER_PER_PURCHASE_CAP in r.decision.reasons
    assert event_types(h).count("mandate_set") == 2


def test_concurrent_requests_cannot_overspend(clock):
    shops = [f"shop{i}@example.com" for i in range(12)]
    h = make_harness(clock=clock, allowed_merchants=tuple(shops), max_purchases_per_day=50,
                     step_up_threshold=Decimal("60"))
    order_ids = [h.order(shop, "38.00") for shop in shops]
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda oid: h.pay(oid), order_ids))
    done = [r for r in results if r.status == Status.COMPLETED]
    assert len(done) == 5 and h.mock.captured_total == Decimal("190.00")
    assert h.audit.verify().ok


def test_status_view(clock):
    h = make_harness(clock=clock)
    h.pay(h.order(PIZZA, "24.50"))
    st = h.guard.status()
    assert st["spent_in_window"] == Decimal("24.50") and st["remaining_budget"] == Decimal("175.50")
    assert st["audit_events"] == len(h.audit.events()) and not st["kill_switch"]


def test_a_yes_only_counts_for_the_order_the_owner_was_shown():
    """The bug found in the first real run: owner sees $45, merchant makes it $58 while waiting, owner says yes."""
    h = make_harness()
    oid = h.order(SUSHI, "45.00")
    waiting = h.pay(oid)
    h.mock.amend_order_amount(oid, Decimal("58.00"))
    answer = h.guard.approve(waiting.request_id, approver="Sam")
    assert answer.status == Status.BLOCKED and Reason.ORDER_CHANGED in answer.decision.reasons
    assert "45.00" in answer.decision.messages[0] and "58.00" in answer.decision.messages[0]
    assert h.mock.captured_total == 0 and h.guard.status()["spent_in_window"] == 0
    assert "payment_authorized" not in event_types(h)


def test_a_changed_payee_while_waiting_is_blocked_too():
    h = make_harness()
    oid = h.order(SUSHI, "45.00")
    waiting = h.pay(oid)
    h.mock._orders[oid]["payee"] = PIZZA  # still an allowed merchant, but not the one the owner was shown
    answer = h.guard.approve(waiting.request_id)
    assert answer.status == Status.BLOCKED and "person being paid is different" in answer.decision.messages[0]


def test_an_unchanged_order_is_still_approved_normally():
    h = make_harness()
    waiting = h.pay(h.order(SUSHI, "45.00"))
    assert h.guard.approve(waiting.request_id).status == Status.COMPLETED


def test_the_shown_order_is_remembered_across_a_restart(tmp_path):
    path = tmp_path / "audit.jsonl"
    h = make_harness(log_path=path)
    oid = h.order(SUSHI, "45.00")
    waiting = h.pay(oid)
    h.mock.amend_order_amount(oid, Decimal("58.00"))
    fresh = make_harness(log_path=path, clock=h.clock, mock=h.mock, with_mandate=False)  # the program was restarted
    assert fresh.guard.approve(waiting.request_id).status == Status.BLOCKED
    assert h.mock.captured_total == 0
