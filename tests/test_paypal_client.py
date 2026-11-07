"""The real client, tested against a FAKE PayPal server (no network). These prove the requests we send
and the replies we understand match PayPal's documented shapes; they cannot prove PayPal agrees."""
import base64
import json
from decimal import Decimal

import httpx
import pytest

from leash.paypal_api import (PayPalClient, PayPalError, auth_assertion, parse_capture, parse_dispute, parse_order)

ORDER = {
    "id": "5O190127TN364715T", "status": "PAYER_ACTION_REQUIRED", "create_time": "2026-10-21T09:00:00Z",
    "purchase_units": [{"reference_id": "leash-abc", "custom_id": "leash:abc", "description": "Team lunch",
                        "amount": {"currency_code": "USD", "value": "24.50"},
                        "payee": {"email_address": "pizza-palace@example.com", "merchant_id": "MERCHANT123"}}],
    "links": [{"href": "https://api-m.sandbox.paypal.com/v2/checkout/orders/5O190127TN364715T", "rel": "self", "method": "GET"},
              {"href": "https://www.sandbox.paypal.com/checkoutnow?token=5O190127TN364715T", "rel": "payer-action", "method": "GET"}],
}
CAPTURED = {**ORDER, "status": "COMPLETED", "purchase_units": [{**ORDER["purchase_units"][0], "payments": {"captures": [
    {"id": "3C679366HH908993F", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "24.50"},
     "create_time": "2026-10-21T09:05:00Z"}]}}]}


class Server:
    """A tiny fake PayPal: records every request and answers from a route table."""

    def __init__(self):
        self.requests = []
        self.routes = {}
        self.token_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/v1/oauth2/token":
            self.token_calls += 1
            return httpx.Response(200, json={"access_token": f"TOKEN{self.token_calls}", "token_type": "Bearer", "expires_in": 32400})
        raw = request.url.raw_path.decode().split("?")[0]  # the path exactly as sent on the wire
        key = (request.method, raw) if (request.method, raw) in self.routes else (request.method, request.url.path)
        handler = self.routes[key]
        return handler(request) if callable(handler) else handler

    def api_calls(self):
        return [r for r in self.requests if r.url.path != "/v1/oauth2/token"]


def make(server, **kw):
    clock = kw.pop("clock", None)
    extra = {"clock": clock} if clock else {}
    return PayPalClient("CID", "SECRET", http=httpx.Client(transport=httpx.MockTransport(server)),
                        sleep=lambda s: None, **extra, **kw)


def test_token_request_uses_basic_auth_and_client_credentials():
    server = Server()
    server.routes[("GET", "/v2/checkout/orders/ORD1")] = lambda r: httpx.Response(200, json=ORDER)
    make(server).get_order("ORD1")
    token_req = server.requests[0]
    assert token_req.headers["authorization"] == "Basic " + base64.b64encode(b"CID:SECRET").decode()
    assert token_req.content == b"grant_type=client_credentials"
    assert server.api_calls()[0].headers["authorization"] == "Bearer TOKEN1"


def test_token_is_cached_then_refreshed_after_expiry():
    server, now = Server(), [0.0]
    server.routes[("GET", "/v2/checkout/orders/ORD1")] = lambda r: httpx.Response(200, json=ORDER)
    client = make(server, clock=lambda: now[0])
    client.get_order("ORD1"); client.get_order("ORD1")
    assert server.token_calls == 1
    now[0] = 40000.0
    client.get_order("ORD1")
    assert server.token_calls == 2


def test_a_401_triggers_one_token_refresh_and_a_retry():
    server, seen = Server(), []

    def handler(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(401, json={"name": "AUTHENTICATION_FAILURE"}) if len(seen) == 1 else httpx.Response(200, json=ORDER)

    server.routes[("GET", "/v2/checkout/orders/ORD1")] = handler
    assert make(server).get_order("ORD1").order_id == "5O190127TN364715T"
    assert seen == ["Bearer TOKEN1", "Bearer TOKEN2"]


def test_parse_order_reads_the_documented_shape():
    f = parse_order(ORDER)
    assert f.order_id == "5O190127TN364715T" and f.status == "PAYER_ACTION_REQUIRED"
    assert f.amount == Decimal("24.50") and f.currency == "USD" and f.unit_count == 1
    assert set(f.payee_ids) == {"MERCHANT123", "pizza-palace@example.com"}
    assert f.approval_url.endswith("token=5O190127TN364715T")
    assert parse_order(CAPTURED).capture_ids == ("3C679366HH908993F",)


def test_parse_order_survives_missing_pieces():
    f = parse_order({"id": "X12345", "status": "CREATED"})
    assert f.amount is None and f.unit_count == 0 and f.payee_ids == () and f.approval_url is None
    assert parse_order({"id": "X12345", "purchase_units": [{"amount": {"value": "oops", "currency_code": "USD"}}]}).amount is None


def test_create_order_sends_the_documented_body_and_headers():
    server, bodies = Server(), []

    def create(request):
        bodies.append((json.loads(request.content), dict(request.headers)))
        return httpx.Response(201, json=ORDER)

    server.routes[("POST", "/v2/checkout/orders")] = create
    facts = make(server, return_url="https://shop.test/ok", cancel_url="https://shop.test/no").create_order(
        request_id="req-1", payee="pizza-palace@example.com", amount=Decimal("24.5"), currency="USD",
        description="Team lunch", custom_id="leash:abc")
    body, headers = bodies[0]
    unit = body["purchase_units"][0]
    assert body["intent"] == "CAPTURE"
    assert unit["amount"] == {"currency_code": "USD", "value": "24.50"}
    assert unit["payee"] == {"email_address": "pizza-palace@example.com"} and unit["custom_id"] == "leash:abc"
    ctx = body["payment_source"]["paypal"]["experience_context"]
    assert ctx["return_url"] == "https://shop.test/ok" and ctx["user_action"] == "PAY_NOW"
    assert headers["paypal-request-id"] == "req-1" and headers["content-type"] == "application/json"
    assert facts.amount == Decimal("24.50")


def test_create_order_with_a_merchant_id_payee_and_a_minimal_reply():
    server, bodies = Server(), []
    minimal = {"id": "5O190127TN364715T", "status": "PAYER_ACTION_REQUIRED", "links": ORDER["links"]}

    def create(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json=minimal)

    server.routes[("POST", "/v2/checkout/orders")] = create
    server.routes[("GET", "/v2/checkout/orders/5O190127TN364715T")] = lambda r: httpx.Response(200, json=ORDER)
    facts = make(server).create_order(request_id="r2", payee="MERCHANT123", amount=Decimal("24.50"), currency="USD")
    assert bodies[0]["purchase_units"][0]["payee"] == {"merchant_id": "MERCHANT123"}
    assert facts.amount == Decimal("24.50")  # filled in by a follow-up GET


def test_capture_parses_and_sends_an_idempotency_key():
    server, seen = Server(), []

    def capture(request):
        seen.append(dict(request.headers))
        return httpx.Response(201, json=CAPTURED)

    server.routes[("POST", "/v2/checkout/orders/ORD1/capture")] = capture
    cap = make(server).capture_order("ORD1", request_id="req-1-capture")
    assert cap.capture_id == "3C679366HH908993F" and cap.status == "COMPLETED" and cap.amount == Decimal("24.50")
    assert seen[0]["paypal-request-id"] == "req-1-capture"


def test_capture_without_a_capture_in_the_reply_is_an_error():
    with pytest.raises(PayPalError):
        parse_capture({"id": "X", "status": "COMPLETED", "purchase_units": [{}]})


def test_errors_are_translated():
    server = Server()
    server.routes[("POST", "/v2/checkout/orders/ORD1/capture")] = httpx.Response(422, json={
        "name": "UNPROCESSABLE_ENTITY", "message": "not approved", "debug_id": "dbg1",
        "details": [{"issue": "ORDER_NOT_APPROVED", "description": "..."}]})
    with pytest.raises(PayPalError) as info:
        make(server).capture_order("ORD1", request_id="r")
    err = info.value
    assert err.status == 422 and err.checkout_not_approved and err.debug_id == "dbg1" and "ORDER_NOT_APPROVED" in err.issues


def test_bad_credentials_give_a_clear_error():
    def refuse(request):
        return httpx.Response(401, json={"error": "invalid_client", "error_description": "Client Authentication failed"})

    client = PayPalClient("CID", "SECRET", http=httpx.Client(transport=httpx.MockTransport(refuse)), sleep=lambda s: None)
    with pytest.raises(PayPalError, match="access token"):
        client.get_order("ORD1")


def test_safe_reads_are_retried_but_a_hard_error_is_not():
    server, calls = Server(), []

    def flaky(request):
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json=ORDER)

    server.routes[("GET", "/v2/checkout/orders/ORD1")] = flaky
    assert make(server).get_order("ORD1").order_id and len(calls) == 3
    server.routes[("GET", "/v2/checkout/orders/ORD2")] = httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"})
    with pytest.raises(PayPalError):
        make(server).get_order("ORD2")


def test_writes_without_an_idempotency_key_are_never_retried():
    server, calls = Server(), []

    def boom(request):
        calls.append(1)
        return httpx.Response(503)

    server.routes[("POST", "/v1/customer/disputes")] = boom
    with pytest.raises(PayPalError):
        make(server).create_sandbox_dispute(buyer_email="b@personal.example.com", buyer_transaction_id="TX1", amount=Decimal("5"))
    assert len(calls) == 1


def test_writes_with_an_idempotency_key_are_retried():
    server, calls = Server(), []

    def flaky(request):
        calls.append(1)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(201, json=CAPTURED)

    server.routes[("POST", "/v2/checkout/orders/ORD1/capture")] = flaky
    assert make(server).capture_order("ORD1", request_id="r").capture_id and len(calls) == 2


def test_ids_are_url_encoded_so_they_cannot_change_the_path():
    server = Server()
    server.routes[("GET", "/v2/checkout/orders/..%2F..%2Fadmin")] = httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"})
    with pytest.raises(PayPalError):
        make(server).get_order("../../admin")
    sent = server.api_calls()[0].url.raw_path.decode()
    assert "/../" not in sent and "%2F" in sent


def test_live_urls_are_refused_unless_forced():
    with pytest.raises(ValueError, match="SANDBOX"):
        PayPalClient("a", "b", base_url="https://api-m.paypal.com")
    live = PayPalClient("a", "b", base_url="https://api-m.paypal.com", allow_live=True)
    with pytest.raises(PayPalError, match="sandbox"):
        live.create_sandbox_dispute(buyer_email="x@y.z", buyer_transaction_id="T", amount=Decimal("1"))


def test_sandbox_dispute_creation_matches_the_documented_request():
    server, seen = Server(), []

    def create(request):
        seen.append(request)
        return httpx.Response(201, json={"links": [{"href": "https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-D-27803", "rel": "self"}]})

    server.routes[("POST", "/v1/customer/disputes")] = create
    did = make(server).create_sandbox_dispute(buyer_email="sb-buyer@personal.example.com", buyer_transaction_id="TX123", amount=Decimal("24.5"))
    req = seen[0]
    assert did == "PP-D-27803"
    assert req.headers["content-type"].startswith("multipart/form-data")
    assert b'name="input"' in req.content and b"application/json" in req.content
    assert b'"buyer_transaction_id": "TX123"' in req.content and b'"value": "24.50"' in req.content
    header, payload, signature = req.headers["paypal-auth-assertion"].split(".")
    assert signature == ""
    pad = lambda s: s + "=" * (-len(s) % 4)
    assert json.loads(base64.urlsafe_b64decode(pad(header))) == {"alg": "none", "typ": "JWT"}
    assert json.loads(base64.urlsafe_b64decode(pad(payload))) == {"iss": "CID", "email": "sb-buyer@personal.example.com"}


def test_auth_assertion_matches_the_prefix_in_paypals_own_sample():
    assert auth_assertion("X", email="a@b.c").startswith("eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.")  # {"alg":"none","typ":"JWT"}


def test_list_and_get_disputes_parse():
    server = Server()
    detail = {"dispute_id": "PP-D-27803", "status": "OPEN", "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
              "dispute_amount": {"currency_code": "USD", "value": "24.50"}, "dispute_life_cycle_stage": "INQUIRY",
              "disputed_transactions": [{"buyer_transaction_id": "BTX1", "seller_transaction_id": "3C679366HH908993F"}]}
    seen = []
    server.routes[("GET", "/v1/customer/disputes")] = lambda r: (seen.append(dict(r.url.params)) or httpx.Response(200, json={"items": [{"dispute_id": "PP-D-27803", "status": "OPEN"}]}))
    server.routes[("GET", "/v1/customer/disputes/PP-D-27803")] = httpx.Response(200, json=detail)
    client = make(server)
    assert client.list_disputes(transaction_id="3C679366HH908993F")[0].dispute_id == "PP-D-27803"
    assert seen[0]["disputed_transaction_id"] == "3C679366HH908993F"
    d = client.get_dispute("PP-D-27803")
    assert d.amount == Decimal("24.50") and set(d.transaction_ids) == {"BTX1", "3C679366HH908993F"}
    assert parse_dispute(detail).life_cycle_stage == "INQUIRY"


def test_a_checkout_link_is_supplied_when_paypal_omits_it():
    server = Server()
    no_links = {k: v for k, v in ORDER.items() if k != "links"}
    server.routes[("GET", "/v2/checkout/orders/ORD1")] = lambda r: httpx.Response(200, json=no_links)
    server.routes[("GET", "/v2/checkout/orders/ORD2")] = lambda r: httpx.Response(200, json={**no_links, "status": "COMPLETED"})
    client = make(server)
    assert client.get_order("ORD1").approval_url == "https://www.sandbox.paypal.com/checkoutnow?token=5O190127TN364715T"
    assert client.get_order("ORD2").approval_url is None  # nothing to approve on a finished order
