"""The data shapes used everywhere. Money is always Decimal, never float."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .util import canonical_json, new_id, norm_id, sha256_hex, utcnow

SUPPORTED_CURRENCIES = {"USD", "EUR", "GBP", "AUD", "CAD"}  # all use 2 decimal places
TWO_PLACES = Decimal("0.01")
MAX_AMOUNT = Decimal("1000000")
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{5,64}$")
CURRENCY_SYMBOL = {"USD": "$", "EUR": "EUR ", "GBP": "GBP ", "AUD": "A$", "CAD": "C$"}


def check_amount(value: Any) -> Decimal:
    """Accept only a positive, finite amount with at most 2 decimal places."""
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
        if not amount.is_finite():
            raise ValueError("amount must be a real number")
        if amount <= 0:
            raise ValueError("amount must be more than zero")
        if amount > MAX_AMOUNT:
            raise ValueError("amount is unreasonably large")
        if amount != amount.quantize(TWO_PLACES):
            raise ValueError("amount can have at most 2 decimal places")
        return amount.quantize(TWO_PLACES)
    except InvalidOperation as exc:
        raise ValueError("amount is not a valid number") from exc


class Verdict(str, Enum):
    ALLOW = "allow"
    ESCALATE = "escalate"  # needs the owner's OK first
    BLOCK = "block"


class Reason(str, Enum):
    OK = "ok"
    NO_MANDATE = "no_mandate"
    KILL_SWITCH = "kill_switch_on"
    MANDATE_NOT_STARTED = "mandate_not_started"
    MANDATE_EXPIRED = "mandate_expired"
    INVALID_REQUEST = "invalid_request"
    UNVERIFIABLE_ORDER = "unverifiable_order"
    UNSUPPORTED_ORDER = "unsupported_order"
    ORDER_NOT_PAYABLE = "order_not_payable"
    DUPLICATE_ORDER = "duplicate_order"
    ORDER_CHANGED = "order_changed_after_approval"
    CLAIM_MISMATCH = "claim_does_not_match_paypal"
    CURRENCY_MISMATCH = "currency_mismatch"
    MERCHANT_NOT_ALLOWED = "merchant_not_allowed"
    LOOKALIKE_MERCHANT = "lookalike_merchant"
    OVER_PER_PURCHASE_CAP = "over_per_purchase_cap"
    OVER_PERIOD_BUDGET = "over_period_budget"
    TOO_MANY_PURCHASES = "too_many_purchases_today"
    NEEDS_OWNER_APPROVAL = "needs_owner_approval"
    SPLIT_PURCHASE_SUSPECTED = "split_purchase_suspected"
    POSSIBLE_DUPLICATE = "possible_duplicate"


# "Soft" reasons can be unlocked by the owner saying yes. Every other reason is a hard block:
# to go beyond a hard limit the owner must change the mandate (which is logged).
SOFT_REASONS = {
    Reason.NEEDS_OWNER_APPROVAL,
    Reason.SPLIT_PURCHASE_SUSPECTED,
    Reason.POSSIBLE_DUPLICATE,
}


class Status(str, Enum):
    BLOCKED = "blocked"
    NEEDS_OWNER_APPROVAL = "needs_owner_approval"
    AWAITING_CHECKOUT = "awaiting_paypal_checkout_approval"
    COMPLETED = "completed"
    REJECTED = "rejected_by_owner"
    FAILED = "failed"


class Mandate(BaseModel):
    """The rules the human owner sets. The agent never edits these."""

    model_config = ConfigDict(frozen=True)

    mandate_id: str = Field(default_factory=new_id)
    owner: str
    currency: str = "USD"
    per_purchase_cap: Decimal
    budget_amount: Decimal
    budget_window_hours: int = 168
    max_purchases_per_day: int = 3
    step_up_threshold: Decimal
    allowed_merchants: tuple[str, ...]
    valid_from: datetime
    valid_until: datetime
    note: str = ""

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: str) -> str:
        v = v.upper().strip()
        if v not in SUPPORTED_CURRENCIES:
            raise ValueError(f"currency must be one of {sorted(SUPPORTED_CURRENCIES)}")
        return v

    @field_validator("per_purchase_cap", "budget_amount", "step_up_threshold")
    @classmethod
    def _money(cls, v: Decimal) -> Decimal:
        return check_amount(v)

    @field_validator("allowed_merchants")
    @classmethod
    def _merchants(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        out: list[str] = []
        for item in v:
            n = norm_id(item)
            if not n:
                raise ValueError("merchant ids must not be empty")
            if n not in out:
                out.append(n)
        if not out:
            raise ValueError("allowed_merchants must not be empty")
        return tuple(out)

    @field_validator("valid_from", "valid_until")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("datetimes must include a timezone")
        return v.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _relations(self) -> "Mandate":
        if self.step_up_threshold > self.per_purchase_cap:
            raise ValueError("step_up_threshold cannot be above per_purchase_cap")
        if self.per_purchase_cap > self.budget_amount:
            raise ValueError("per_purchase_cap cannot be above budget_amount")
        if self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be after valid_from")
        if not 1 <= self.budget_window_hours <= 24 * 31:
            raise ValueError("budget_window_hours must be between 1 and 744")
        if not 1 <= self.max_purchases_per_day <= 50:
            raise ValueError("max_purchases_per_day must be between 1 and 50")
        return self

    def fingerprint(self) -> str:
        return sha256_hex(canonical_json(self))

    def describe(self) -> list[str]:
        sym = CURRENCY_SYMBOL.get(self.currency, "")
        days = self.budget_window_hours / 24
        return [
            f"Owner: {self.owner}",
            f"Each purchase: at most {sym}{self.per_purchase_cap}",
            f"Total spending: at most {sym}{self.budget_amount} in any {days:g} days",
            f"Purchases per day: at most {self.max_purchases_per_day}",
            f"Asks you first for purchases of {sym}{self.step_up_threshold} or more",
            f"Allowed merchants: {', '.join(self.allowed_merchants)}",
            f"Valid until: {self.valid_until:%Y-%m-%d %H:%M} UTC",
        ]


class PurchaseRequest(BaseModel):
    """What the AI agent asks for. Everything here is a CLAIM: Leash checks it against PayPal."""

    request_id: str = Field(default_factory=new_id)
    order_id: str
    claimed_merchant: Optional[str] = None
    claimed_amount: Optional[Decimal] = None
    claimed_currency: Optional[str] = None
    description: str = ""
    agent_id: str = "agent"
    source_context: str = ""  # untrusted text (e.g. a web page) that led to this request
    requested_at: datetime = Field(default_factory=utcnow)

    @field_validator("request_id", "order_id")
    @classmethod
    def _safe_id(cls, v: str) -> str:
        if not SAFE_ID.match(v):
            raise ValueError("ids may only contain letters, numbers, - and _ (5 to 64 characters)")
        return v

    @field_validator("claimed_amount")
    @classmethod
    def _claimed_amount(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        return None if v is None else check_amount(v)

    @field_validator("description", mode="before")
    @classmethod
    def _cut_description(cls, v: Any) -> Any:
        return v[:500] if isinstance(v, str) else v

    @field_validator("source_context", mode="before")
    @classmethod
    def _cut_context(cls, v: Any) -> Any:
        return v[:2000] if isinstance(v, str) else v

    @field_validator("agent_id", mode="before")
    @classmethod
    def _cut_agent(cls, v: Any) -> Any:
        return v[:64] if isinstance(v, str) else v


class Decision(BaseModel):
    verdict: Verdict
    reasons: list[Reason]
    messages: list[str]
    approved_flags: list[Reason] = Field(default_factory=list)
    mandate_fingerprint: str = ""
    spent_in_window: Decimal = Decimal("0")
    purchases_today: int = 0
    verified_amount: Optional[Decimal] = None
    verified_currency: Optional[str] = None
    verified_payee_ids: list[str] = Field(default_factory=list)
    order_status: Optional[str] = None
    checked_at: datetime = Field(default_factory=utcnow)


class PurchaseResult(BaseModel):
    request_id: str
    status: Status
    decision: Decision
    order_id: Optional[str] = None
    capture_id: Optional[str] = None
    approval_url: Optional[str] = None
    detail: str = ""


# ---- Facts that come back from PayPal (or the mock) ---------------------------------------

class OrderFacts(BaseModel):
    order_id: str
    status: str
    amount: Optional[Decimal] = None
    currency: Optional[str] = None
    payee_ids: tuple[str, ...] = ()
    unit_count: int = 0
    approval_url: Optional[str] = None
    capture_ids: tuple[str, ...] = ()
    description: str = ""
    custom_id: str = ""
    created_time: Optional[str] = None


class CaptureFacts(BaseModel):
    capture_id: str
    order_id: str
    status: str
    amount: Optional[Decimal] = None
    currency: Optional[str] = None
    created_time: Optional[str] = None


class DisputeFacts(BaseModel):
    dispute_id: str
    status: Optional[str] = None
    reason: Optional[str] = None
    amount: Optional[Decimal] = None
    currency: Optional[str] = None
    transaction_ids: tuple[str, ...] = ()
    create_time: Optional[str] = None
    update_time: Optional[str] = None
    life_cycle_stage: Optional[str] = None
