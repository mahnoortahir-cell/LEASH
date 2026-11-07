"""Talking to PayPal.

* PayPalLike   - the few calls Leash needs. The real client and the mock both provide them.
* PayPalClient - the real client for PayPal's REST API (SANDBOX only unless you force it).

IMPORTANT: PayPalClient was written from PayPal's public docs and is tested offline with a
faked HTTP layer. It has NOT been run against the live PayPal sandbox by its author.
Run `python -m leash sandbox check` first; if a field name differs, the fix is in this file.
"""
from __future__ import annotations

import base64
import json
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional, Protocol
from urllib.parse import quote

import httpx

from .models import CaptureFacts, DisputeFacts, OrderFacts


class PayPalError(Exception):
    def __init__(self, message: str, *, status: Optional[int] = None, name: str = "",
                 issues: tuple[str, ...] = (), debug_id: str = ""):
        super().__init__(message)
        self.status = status
        self.name = name
        self.issues = tuple(issues)
        self.debug_id = debug_id

    @property
    def checkout_not_approved(self) -> bool:
        return bool({"ORDER_NOT_APPROVED", "PAYER_ACTION_REQUIRED"} & set(self.issues))

    def to_dict(self) -> dict[str, Any]:
        return {"message": str(self), "status": self.status, "name": self.name,
                "issues": list(self.issues), "debug_id": self.debug_id}


class PayPalLike(Protocol):
    def create_order(self, *, request_id: str, payee: Optional[str], amount: Decimal, currency: str,
                     description: str = "", custom_id: str = "") -> OrderFacts: ...

    def get_order(self, order_id: str) -> OrderFacts: ...

    def capture_order(self, order_id: str, *, request_id: str) -> CaptureFacts: ...

    def list_disputes(self, *, transaction_id: Optional[str] = None) -> list[DisputeFacts]: ...

    def get_dispute(self, dispute_id: str) -> DisputeFacts: ...


# ---- Turning PayPal JSON into our small, safe "facts" objects --------------------------------

def _decimal(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def parse_order(data: dict[str, Any]) -> OrderFacts:
    units = data.get("purchase_units") or []
    unit = units[0] if units else {}
    amount = unit.get("amount") or {}
    payee = unit.get("payee") or {}
    payee_ids = tuple(x for x in (payee.get("merchant_id"), payee.get("email_address")) if x)
    links = {link.get("rel"): link.get("href") for link in (data.get("links") or [])}
    captures = tuple(
        c["id"] for u in units for c in ((u.get("payments") or {}).get("captures") or []) if c.get("id")
    )
    return OrderFacts(
        order_id=data["id"],
        status=data.get("status", ""),
        amount=_decimal(amount.get("value")),
        currency=amount.get("currency_code"),
        payee_ids=payee_ids,
        unit_count=len(units),
        approval_url=links.get("payer-action") or links.get("approve"),
        capture_ids=captures,
        description=unit.get("description") or "",
        custom_id=unit.get("custom_id") or "",
        created_time=data.get("create_time"),
    )


def parse_capture(data: dict[str, Any]) -> CaptureFacts:
    units = data.get("purchase_units") or []
    caps = [c for u in units for c in ((u.get("payments") or {}).get("captures") or [])]
    if not caps:
        raise PayPalError("PayPal returned no capture", name="NO_CAPTURE")
    cap = caps[0]
    amount = cap.get("amount") or {}
    return CaptureFacts(
        capture_id=cap["id"],
        order_id=data.get("id", ""),
        status=cap.get("status") or data.get("status", ""),
        amount=_decimal(amount.get("value")),
        currency=amount.get("currency_code"),
        created_time=cap.get("create_time"),
    )


def parse_dispute(data: dict[str, Any]) -> DisputeFacts:
    tx_ids: list[str] = []
    for tx in data.get("disputed_transactions") or []:
        for key in ("seller_transaction_id", "buyer_transaction_id"):
            if tx.get(key):
                tx_ids.append(tx[key])
    amount = data.get("dispute_amount") or {}
    return DisputeFacts(
        dispute_id=data["dispute_id"],
        status=data.get("status"),
        reason=data.get("reason"),
        amount=_decimal(amount.get("value")),
        currency=amount.get("currency_code"),
        transaction_ids=tuple(tx_ids),
        create_time=data.get("create_time"),
        update_time=data.get("update_time"),
        life_cycle_stage=data.get("dispute_life_cycle_stage"),
    )


def _b64url(obj: dict[str, Any]) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def auth_assertion(client_id: str, *, email: Optional[str] = None, payer_id: Optional[str] = None) -> str:
    """The unsigned JWT PayPal's docs use for PayPal-Auth-Assertion (acting for a sandbox buyer)."""
    payload: dict[str, Any] = {"iss": client_id}
    if email:
        payload["email"] = email
    if payer_id:
        payload["payer_id"] = payer_id
    return f"{_b64url({'alg': 'none', 'typ': 'JWT'})}.{_b64url(payload)}."


# ---- The real client --------------------------------------------------------------------------

class PayPalClient:
    SANDBOX_URL = "https://api-m.sandbox.paypal.com"

    def __init__(self, client_id: str, client_secret: str, *, base_url: str = SANDBOX_URL,
                 allow_live: bool = False, http: Optional[httpx.Client] = None,
                 return_url: str = "https://example.com/return", cancel_url: str = "https://example.com/cancel",
                 brand_name: str = "Leash", sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, max_retries: int = 2):
        if "sandbox" not in base_url and not allow_live:
            raise ValueError(
                "Leash only talks to the PayPal SANDBOX by default. "
                "Pass allow_live=True only if you really mean to touch real money."
            )
        self.base_url = base_url.rstrip("/")
        self.is_sandbox = "sandbox" in self.base_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http or httpx.Client(timeout=20.0)
        self._return_url = return_url
        self._cancel_url = cancel_url
        self._brand_name = brand_name
        self._sleep = sleep
        self._clock = clock
        self._max_retries = max_retries
        self._access_token: Optional[str] = None
        self._token_expiry = 0.0

    # -- plumbing --
    @staticmethod
    def _error_from(resp: httpx.Response, prefix: str = "") -> PayPalError:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        name = body.get("name") or body.get("error") or ""
        message = body.get("message") or body.get("error_description") or resp.text[:200]
        issues = tuple(d["issue"] for d in (body.get("details") or []) if isinstance(d, dict) and d.get("issue"))
        text = f"{prefix}: {message}" if prefix else str(message)
        return PayPalError(text, status=resp.status_code, name=name, issues=issues,
                           debug_id=body.get("debug_id", ""))

    def _token(self) -> str:
        if self._access_token and self._clock() < self._token_expiry:
            return self._access_token
        try:
            resp = self._http.post(
                f"{self.base_url}/v1/oauth2/token", auth=(self._client_id, self._client_secret),
                data={"grant_type": "client_credentials"}, headers={"Accept": "application/json"},
            )
        except httpx.TransportError as exc:
            raise PayPalError(f"network problem while getting a token: {exc}", name="NETWORK") from exc
        if resp.status_code != 200:
            raise self._error_from(resp, "could not get an access token")
        body = resp.json()
        self._access_token = body["access_token"]
        self._token_expiry = self._clock() + max(int(body.get("expires_in", 300)) - 60, 30)
        return self._access_token

    def _request(self, method: str, path: str, *, json_body: Optional[dict[str, Any]] = None,
                 files: Any = None, params: Optional[dict[str, Any]] = None,
                 headers: Optional[dict[str, str]] = None, expect: tuple[int, ...] = (200, 201, 204)) -> dict[str, Any]:
        extra = dict(headers or {})
        # Only retry when it is safe: reads, or writes that carry an idempotency key.
        can_retry = method == "GET" or "PayPal-Request-Id" in extra
        attempt = 0
        refreshed = False
        while True:
            hdrs = {"Authorization": f"Bearer {self._token()}", "Accept": "application/json", **extra}
            if json_body is not None:
                hdrs["Content-Type"] = "application/json"
            try:
                resp = self._http.request(method, f"{self.base_url}{path}", json=json_body, files=files,
                                          params=params, headers=hdrs)
            except httpx.TransportError as exc:
                if can_retry and attempt < self._max_retries:
                    attempt += 1
                    self._sleep(0.5 * attempt)
                    continue
                raise PayPalError(f"network problem: {exc}", name="NETWORK") from exc
            if resp.status_code == 401 and not refreshed:
                self._access_token = None
                refreshed = True
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and can_retry and attempt < self._max_retries:
                attempt += 1
                self._sleep(0.5 * attempt)
                continue
            if resp.status_code not in expect:
                raise self._error_from(resp)
            if not resp.content:
                return {}
            try:
                data = resp.json()
            except ValueError as exc:
                raise PayPalError("PayPal sent a reply that is not JSON", status=resp.status_code) from exc
            return data if isinstance(data, dict) else {"items": data}

    def _with_checkout_link(self, facts: OrderFacts) -> OrderFacts:
        """If PayPal's reply has no buyer link, use the documented checkout URL for unapproved orders."""
        if facts.approval_url is None and facts.status in ("CREATED", "PAYER_ACTION_REQUIRED"):
            web = "https://www.sandbox.paypal.com" if self.is_sandbox else "https://www.paypal.com"
            return facts.model_copy(update={"approval_url": f"{web}/checkoutnow?token={quote(facts.order_id, safe='')}"})
        return facts

    # -- orders (merchant side in real life; Leash's demo plays both roles in the sandbox) --
    def create_order(self, *, request_id: str, payee: Optional[str], amount: Decimal, currency: str,
                     description: str = "", custom_id: str = "") -> OrderFacts:
        unit: dict[str, Any] = {
            "reference_id": f"leash-{request_id[:32]}",
            "custom_id": custom_id[:255],
            "description": description[:127],
            "amount": {"currency_code": currency, "value": f"{amount:.2f}"},
        }
        if payee:
            unit["payee"] = {"email_address": payee} if "@" in payee else {"merchant_id": payee}
        body = {
            "intent": "CAPTURE",
            "purchase_units": [unit],
            "payment_source": {"paypal": {"experience_context": {
                "payment_method_preference": "IMMEDIATE_PAYMENT_REQUIRED",
                "user_action": "PAY_NOW",
                "brand_name": self._brand_name,
                "return_url": self._return_url,
                "cancel_url": self._cancel_url,
            }}},
        }
        data = self._request("POST", "/v2/checkout/orders", json_body=body,
                             headers={"PayPal-Request-Id": request_id, "Prefer": "return=representation"},
                             expect=(200, 201))
        facts = parse_order(data)
        # PayPal may answer with a minimal body; ask again so we always hold the full facts.
        return self._with_checkout_link(facts) if facts.amount is not None else self.get_order(facts.order_id)

    def get_order(self, order_id: str) -> OrderFacts:
        return self._with_checkout_link(
            parse_order(self._request("GET", f"/v2/checkout/orders/{quote(order_id, safe='')}")))

    def capture_order(self, order_id: str, *, request_id: str) -> CaptureFacts:
        data = self._request("POST", f"/v2/checkout/orders/{quote(order_id, safe='')}/capture",
                             json_body={}, headers={"PayPal-Request-Id": request_id,
                                                    "Prefer": "return=representation"}, expect=(200, 201))
        return parse_capture(data)

    # -- disputes --
    def list_disputes(self, *, transaction_id: Optional[str] = None, page_size: int = 20,
                      as_buyer_email: Optional[str] = None) -> list[DisputeFacts]:
        params: dict[str, Any] = {"page_size": page_size}
        if transaction_id:
            params["disputed_transaction_id"] = transaction_id
        headers = {"PayPal-Auth-Assertion": auth_assertion(self._client_id, email=as_buyer_email)} if as_buyer_email else None
        data = self._request("GET", "/v1/customer/disputes", params=params, headers=headers)
        return [parse_dispute(item) for item in data.get("items", [])]

    def get_dispute(self, dispute_id: str, *, as_buyer_email: Optional[str] = None) -> DisputeFacts:
        headers = {"PayPal-Auth-Assertion": auth_assertion(self._client_id, email=as_buyer_email)} if as_buyer_email else None
        return parse_dispute(self._request("GET", f"/v1/customer/disputes/{quote(dispute_id, safe='')}", headers=headers))

    def create_sandbox_dispute(self, *, buyer_email: str, buyer_transaction_id: str, amount: Decimal,
                               currency: str = "USD", reason: str = "MERCHANDISE_OR_SERVICE_NOT_RECEIVED") -> str:
        """Sandbox-only: PayPal's docs describe an endpoint that lets a test buyer open a dispute."""
        if not self.is_sandbox:
            raise PayPalError("test disputes can only be created in the sandbox", name="LIVE_NOT_ALLOWED")
        body = {
            "disputed_transactions": [{"buyer_transaction_id": buyer_transaction_id}],
            "reason": reason,
            "dispute_amount": {"currency_code": currency, "value": f"{amount:.2f}"},
            "extensions": {"merchant_contacted": True, "merchant_contacted_outcome": "NO_RESPONSE",
                           "merchandize_dispute_properties": {"issue_type": "PRODUCT"}},
        }
        data = self._request(
            "POST", "/v1/customer/disputes",
            files={"input": (None, json.dumps(body), "application/json")},
            headers={"PayPal-Auth-Assertion": auth_assertion(self._client_id, email=buyer_email)},
            expect=(200, 201),
        )
        href = next((link["href"] for link in data.get("links", []) if link.get("rel") == "self"), "")
        if not href:
            raise PayPalError("PayPal did not return a dispute link")
        return href.rstrip("/").rsplit("/", 1)[-1]
