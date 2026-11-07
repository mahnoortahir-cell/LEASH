from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from leash.models import Mandate, PurchaseRequest, check_amount

NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)


def make(**over):
    base = dict(owner="Sam", per_purchase_cap=Decimal("60"), budget_amount=Decimal("200"),
                step_up_threshold=Decimal("40"), allowed_merchants=("A@B.com",), valid_from=NOW,
                valid_until=NOW + timedelta(days=1))
    base.update(over)
    return Mandate(**base)


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-5", "0", "0.001", "1e400", "abc", "", "1000000.01", None])
def test_bad_amounts_rejected(bad):
    with pytest.raises(ValueError):
        check_amount(bad)


@pytest.mark.parametrize("good,expected", [("5", "5.00"), ("5.5", "5.50"), (12, "12.00"), (Decimal("0.01"), "0.01")])
def test_good_amounts_accepted(good, expected):
    assert check_amount(good) == Decimal(expected)


def test_mandate_normalises_and_dedupes_merchants():
    m = make(allowed_merchants=("A@B.com", " a@b.COM ", "c@d.com"))
    assert m.allowed_merchants == ("a@b.com", "c@d.com")


@pytest.mark.parametrize("over", [
    dict(step_up_threshold=Decimal("61")), dict(per_purchase_cap=Decimal("201")),
    dict(valid_until=NOW), dict(allowed_merchants=()), dict(currency="XXX"),
    dict(budget_window_hours=0), dict(max_purchases_per_day=0), dict(valid_from=datetime(2026, 1, 1)),
])
def test_mandate_rejects_nonsense(over):
    with pytest.raises(ValidationError):
        make(**over)


def test_mandate_is_frozen_and_fingerprint_changes_with_content():
    m = make()
    with pytest.raises(ValidationError):
        m.owner = "Eve"
    assert m.fingerprint() == m.fingerprint()
    assert m.fingerprint() != make(owner="Eve").fingerprint()


@pytest.mark.parametrize("order_id", ["../../admin", "a b c d e", "x" * 65, "abc", "ORD;DROP", ""])
def test_unsafe_order_ids_rejected(order_id):
    with pytest.raises(ValidationError):
        PurchaseRequest(order_id=order_id)


def test_request_truncates_untrusted_text_and_rejects_bad_claims():
    r = PurchaseRequest(order_id="ORDER12345", description="x" * 9000, source_context="y" * 9000, agent_id="z" * 500)
    assert len(r.description) == 500 and len(r.source_context) == 2000 and len(r.agent_id) == 64
    with pytest.raises(ValidationError):
        PurchaseRequest(order_id="ORDER12345", claimed_amount="-1")
