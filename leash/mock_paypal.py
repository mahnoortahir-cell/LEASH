"""A pretend PayPal that lives in memory. Same calls as the real client, no network needed.

Use it to develop, to run the tests and to demo. It copies the behaviours Leash cares about:
orders start unapproved, only an approved order can be captured, a second capture is refused,
and a merchant can change an order's amount before the buyer approves it.
"""
from __future__ import annotations

import hashlib
import itertools
from decimal import Decimal
from typing import Callable, Optional
from uuid import uuid4

from .models import CaptureFacts, DisputeFacts, OrderFacts
from .paypal_api import PayPalError
from .util import utcnow


class MockPayPal:
    def __init__(self, clock: Callable = utcnow):
        self._clock = clock
        self._orders: dict[str, dict] = {}
        self._by_request: dict[str, str] = {}
        self._captures: dict[str, CaptureFacts] = {}
        self._capture_by_request: dict[str, str] = {}
        self._disputes: dict[str, DisputeFacts] = {}
        self._dispute_no = itertools.count(1)
        self._fail: dict[str, list[PayPalError]] = {}
        self.calls: list[str] = []  # names of every call, so tests can prove what was (not) asked

    # -- test helpers --
    @staticmethod
    def merchant_id_for(email: str) -> str:
        return "MID" + hashlib.sha1(email.lower().encode()).hexdigest()[:10].upper()

    def fail_next(self, method: str, *, issue: str = "INSTRUMENT_DECLINED", status: int = 422,
                  name: str = "UNPROCESSABLE_ENTITY") -> None:
        self._fail.setdefault(method, []).append(
            PayPalError(f"mock failure: {issue}", status=status, name=name, issues=(issue,), debug_id="mock"))

    def _check(self, method: str) -> None:
        self.calls.append(method)
        if self._fail.get(method):
            raise self._fail[method].pop(0)

    @property
    def captured(self) -> list[CaptureFacts]:
        return list(self._captures.values())

    @property
    def captured_total(self) -> Decimal:
        return sum((c.amount or Decimal("0") for c in self._captures.values()), Decimal("0"))

    def _facts(self, order: dict) -> OrderFacts:
        return OrderFacts(
            order_id=order["id"], status=order["status"], amount=order["amount"], currency=order["currency"],
            payee_ids=(self.merchant_id_for(order["payee"]), order["payee"]), unit_count=1,
            approval_url=f"https://www.sandbox.paypal.com/checkoutnow?token={order['id']}",
            capture_ids=tuple(order["captures"]), description=order["description"], custom_id=order["custom_id"],
            created_time=order["created"],
        )

    # -- merchant side --
    def create_order(self, *, request_id: str, payee: Optional[str], amount: Decimal, currency: str,
                     description: str = "", custom_id: str = "") -> OrderFacts:
        self._check("create_order")
        if request_id in self._by_request:  # same idempotency key -> same order, like PayPal
            return self._facts(self._orders[self._by_request[request_id]])
        order_id = "ORD" + uuid4().hex[:14].upper()
        self._orders[order_id] = {
            "id": order_id, "status": "PAYER_ACTION_REQUIRED", "amount": Decimal(amount), "currency": currency,
            "payee": payee or "default-merchant@example.com", "description": description,
            "custom_id": custom_id, "captures": [], "created": self._clock().isoformat(),
        }
        self._by_request[request_id] = order_id
        return self._facts(self._orders[order_id])

    def amend_order_amount(self, order_id: str, new_amount: Decimal) -> None:
        """What a dishonest merchant might do: change the price before the buyer approves."""
        self._orders[order_id]["amount"] = Decimal(new_amount)

    # -- buyer side (a human clicking "Pay" at PayPal) --
    def simulate_checkout_approval(self, order_id: str) -> None:
        self._orders[order_id]["status"] = "APPROVED"

    # -- shared --
    def get_order(self, order_id: str) -> OrderFacts:
        self._check("get_order")
        if order_id not in self._orders:
            raise PayPalError("The specified resource does not exist.", status=404,
                              name="RESOURCE_NOT_FOUND", issues=("INVALID_RESOURCE_ID",))
        return self._facts(self._orders[order_id])

    def capture_order(self, order_id: str, *, request_id: str) -> CaptureFacts:
        self._check("capture_order")
        if request_id in self._capture_by_request:
            return self._captures[self._capture_by_request[request_id]]
        order = self._orders.get(order_id)
        if order is None:
            raise PayPalError("order not found", status=404, name="RESOURCE_NOT_FOUND")
        if order["status"] == "COMPLETED":
            raise PayPalError("order already captured", status=422, name="UNPROCESSABLE_ENTITY",
                              issues=("ORDER_ALREADY_CAPTURED",))
        if order["status"] != "APPROVED":
            raise PayPalError("buyer has not approved this order", status=422, name="UNPROCESSABLE_ENTITY",
                              issues=("ORDER_NOT_APPROVED",))
        cap_id = "CAP" + uuid4().hex[:14].upper()
        cap = CaptureFacts(capture_id=cap_id, order_id=order_id, status="COMPLETED", amount=order["amount"],
                           currency=order["currency"], created_time=self._clock().isoformat())
        order["status"] = "COMPLETED"
        order["captures"].append(cap_id)
        self._captures[cap_id] = cap
        self._capture_by_request[request_id] = cap_id
        return cap

    # -- disputes --
    def open_dispute(self, capture_id: str, reason: str = "MERCHANDISE_OR_SERVICE_NOT_RECEIVED") -> DisputeFacts:
        cap = self._captures[capture_id]
        dispute = DisputeFacts(
            dispute_id=f"PP-D-MOCK-{next(self._dispute_no):04d}", status="OPEN", reason=reason, amount=cap.amount,
            currency=cap.currency, transaction_ids=(capture_id,), create_time=self._clock().isoformat(),
            update_time=self._clock().isoformat(), life_cycle_stage="INQUIRY")
        self._disputes[dispute.dispute_id] = dispute
        return dispute

    def list_disputes(self, *, transaction_id: Optional[str] = None) -> list[DisputeFacts]:
        self._check("list_disputes")
        return [d for d in self._disputes.values() if transaction_id is None or transaction_id in d.transaction_ids]

    def get_dispute(self, dispute_id: str) -> DisputeFacts:
        self._check("get_dispute")
        if dispute_id not in self._disputes:
            raise PayPalError("dispute not found", status=404, name="RESOURCE_NOT_FOUND")
        return self._disputes[dispute_id]
