"""The rules engine. No AI in here: plain, predictable code that cannot be talked into things.

evaluate() looks at three things and nothing else:
  1. the owner's Mandate (the rules),
  2. the order as PayPal itself reports it (NOT what the agent says about it),
  3. what has already been spent (the ledger).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Optional, Sequence

from .models import (Decision, Mandate, OrderFacts, PurchaseRequest, Reason, Verdict, check_amount)
from .util import norm_id

PAYABLE_STATUSES = {"CREATED", "PAYER_ACTION_REQUIRED", "APPROVED"}
DUPLICATE_WINDOW = timedelta(minutes=10)
LOOKALIKE_RATIO = 0.85
_CONFUSABLE = str.maketrans({"0": "o", "1": "l", "3": "e", "5": "s"})


@dataclass(frozen=True)
class SpendRecord:
    """Money that is committed (captured) or reserved (authorised, not yet captured)."""
    request_id: str
    order_id: str
    payee_ids: frozenset  # normalised ids of who gets paid
    amount: Decimal
    currency: str
    at: datetime
    state: str  # "pending" or "committed"


def _skeleton(text: str) -> str:
    text = text.translate(_CONFUSABLE).replace("rn", "m").replace("vv", "w").replace("i", "l")
    return "".join(ch for ch in text if ch.isalnum() or ch == "@")


def _lookalike_reason(candidates: set, allowed: Sequence[str]) -> Optional[str]:
    for cand in sorted(candidates):
        if any(ord(ch) > 127 for ch in cand):
            return f"'{cand}' contains unusual (non-ASCII) characters, a common phishing trick"
        for ok in allowed:
            if cand != ok and (SequenceMatcher(None, cand, ok).ratio() >= LOOKALIKE_RATIO
                               or _skeleton(cand) == _skeleton(ok)):
                return f"'{cand}' looks almost identical to the allowed merchant '{ok}'"
    return None


def evaluate(mandate: Mandate, request: PurchaseRequest, order: Optional[OrderFacts],
             ledger: Sequence[SpendRecord], now: datetime, *, killed: bool = False,
             human_approved: bool = False) -> Decision:
    window_start = now - timedelta(hours=mandate.budget_window_hours)
    day_start = now - timedelta(hours=24)
    same_currency = [r for r in ledger if r.currency == mandate.currency]
    spent = sum((r.amount for r in same_currency if r.at > window_start), Decimal("0"))
    today = [r for r in same_currency if r.at > day_start]

    base: dict = dict(mandate_fingerprint=mandate.fingerprint(), spent_in_window=spent,
                      purchases_today=len(today), checked_at=now)
    if order is not None:
        base.update(verified_amount=order.amount, verified_currency=order.currency,
                    verified_payee_ids=list(order.payee_ids), order_status=order.status)

    def finish(hard: list, soft: list) -> Decision:
        if hard:
            verdict, used = Verdict.BLOCK, hard
        elif soft and not human_approved:
            verdict, used = Verdict.ESCALATE, soft
        else:
            verdict, used = Verdict.ALLOW, []
        approved = [r for r, _ in soft] if (verdict == Verdict.ALLOW and human_approved) else []
        if approved:
            messages = ["Approved by the owner, who said yes to: " + " ".join(m for _, m in soft)]
        else:
            messages = [m for _, m in used] or ["Within all of the owner's limits."]
        return Decision(verdict=verdict, reasons=[r for r, _ in used] or [Reason.OK], messages=messages,
                        approved_flags=approved, **base)

    # --- global switches: if any is off, stop right here ---
    if killed:
        return finish([(Reason.KILL_SWITCH, "The kill switch is on, so no payments are allowed.")], [])
    if now < mandate.valid_from:
        return finish([(Reason.MANDATE_NOT_STARTED, "The owner's mandate has not started yet.")], [])
    if now >= mandate.valid_until:
        return finish([(Reason.MANDATE_EXPIRED, "The owner's mandate has expired.")], [])
    if order is None:
        return finish([(Reason.UNVERIFIABLE_ORDER,
                        "Could not look this order up at PayPal, so it cannot be checked.")], [])

    hard: list = []
    soft: list = []

    # --- what PayPal says the order really is ---
    if order.unit_count != 1:
        hard.append((Reason.UNSUPPORTED_ORDER,
                     f"The order has {order.unit_count} purchase units; only single-unit orders are supported."))
    if order.status not in PAYABLE_STATUSES:
        hard.append((Reason.ORDER_NOT_PAYABLE, f"The order status is {order.status}, which cannot be paid."))
    if order.amount is None or order.currency is None:
        return finish(hard + [(Reason.UNVERIFIABLE_ORDER, "PayPal did not report an amount for this order.")], [])
    try:
        amount = check_amount(order.amount)
    except ValueError as exc:
        return finish(hard + [(Reason.UNVERIFIABLE_ORDER, f"The amount PayPal reports is not usable: {exc}.")], [])
    payee_norm = {norm_id(p) for p in order.payee_ids}
    if not payee_norm:
        hard.append((Reason.UNVERIFIABLE_ORDER, "PayPal did not say who gets paid."))
    if any(r.order_id == order.order_id for r in ledger):
        hard.append((Reason.DUPLICATE_ORDER, "This PayPal order is already being paid."))

    # --- does the agent's story match PayPal's record? ---
    if request.claimed_amount is not None and request.claimed_amount != amount:
        hard.append((Reason.CLAIM_MISMATCH,
                     f"The agent said {request.claimed_amount} but PayPal says {amount}."))
    if request.claimed_currency and request.claimed_currency.upper() != order.currency:
        hard.append((Reason.CLAIM_MISMATCH,
                     f"The agent said {request.claimed_currency} but PayPal says {order.currency}."))
    if request.claimed_merchant and norm_id(request.claimed_merchant) not in payee_norm:
        hard.append((Reason.CLAIM_MISMATCH,
                     f"The agent said it was paying '{request.claimed_merchant}' but PayPal says the money "
                     f"goes to {', '.join(order.payee_ids) or 'an unknown account'}."))

    # --- the owner's hard limits ---
    if order.currency != mandate.currency:
        hard.append((Reason.CURRENCY_MISMATCH,
                     f"The order is in {order.currency} but the mandate is in {mandate.currency}."))
    allowed = set(mandate.allowed_merchants)
    if payee_norm and not (payee_norm & allowed):
        hard.append((Reason.MERCHANT_NOT_ALLOWED,
                     f"{', '.join(order.payee_ids)} is not on the allowed merchant list."))
        look = _lookalike_reason(payee_norm, mandate.allowed_merchants)
        if look:
            hard.append((Reason.LOOKALIKE_MERCHANT, look + "."))
    if amount > mandate.per_purchase_cap:
        hard.append((Reason.OVER_PER_PURCHASE_CAP,
                     f"{amount} is above the per-purchase cap of {mandate.per_purchase_cap}."))
    if spent + amount > mandate.budget_amount:
        hard.append((Reason.OVER_PERIOD_BUDGET,
                     f"{amount} would take spending to {spent + amount}, above the budget of "
                     f"{mandate.budget_amount} for {mandate.budget_window_hours / 24:g} days."))
    if len(today) >= mandate.max_purchases_per_day:
        hard.append((Reason.TOO_MANY_PURCHASES,
                     f"Already {len(today)} purchases in the last 24 hours (limit {mandate.max_purchases_per_day})."))

    # --- soft flags: the owner can say yes to these ---
    if amount >= mandate.step_up_threshold:
        soft.append((Reason.NEEDS_OWNER_APPROVAL,
                     f"{amount} is at or above {mandate.step_up_threshold}, so the owner must say yes first."))
    else:
        merchant_today = sum((r.amount for r in today if r.payee_ids & payee_norm), Decimal("0"))
        if merchant_today > 0 and merchant_today + amount >= mandate.step_up_threshold:
            soft.append((Reason.SPLIT_PURCHASE_SUSPECTED,
                         f"{merchant_today} was already spent at this merchant in 24 hours; adding {amount} "
                         f"looks like one big purchase split into pieces."))
    if any(r.payee_ids & payee_norm and r.amount == amount and now - r.at <= DUPLICATE_WINDOW for r in same_currency):
        soft.append((Reason.POSSIBLE_DUPLICATE,
                     "The same amount was paid to the same merchant in the last 10 minutes."))

    return finish(hard, soft)
