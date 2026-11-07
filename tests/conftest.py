from decimal import Decimal

import pytest

from leash.clock import FakeClock
from leash.models import OrderFacts
from leash.scenarios import PIZZA, demo_mandate, make_harness


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def world(clock):
    return make_harness(clock=clock)


@pytest.fixture
def mandate(clock):
    return demo_mandate(clock)


def facts(amount="20.00", payee=("MID1", PIZZA), status="PAYER_ACTION_REQUIRED", currency="USD", units=1,
          order_id="ORDER00001"):
    return OrderFacts(order_id=order_id, status=status, amount=None if amount is None else Decimal(amount),
                      currency=currency, payee_ids=tuple(payee), unit_count=units)


@pytest.fixture(autouse=True)
def no_real_ai_or_paypal_keys(monkeypatch):
    """Tests must never pick up real keys from the developer's shell."""
    for name in ("ANTHROPIC_API_KEY", "PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "LEASH_AUDIT_KEY"):
        monkeypatch.delenv(name, raising=False)
