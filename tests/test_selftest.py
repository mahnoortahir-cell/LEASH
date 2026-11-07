import io

import httpx
from rich.console import Console

from leash.selftest import run_selftest
from tests.test_paypal_client import ORDER, Server, make

ONE_DOLLAR = {**ORDER, "purchase_units": [{**ORDER["purchase_units"][0], "amount": {"currency_code": "USD", "value": "1.00"}}]}


def run(server):
    out = io.StringIO()
    code = run_selftest(make(server), Console(file=out, width=200, color_system=None))
    return code, out.getvalue()


def healthy_server():
    server = Server()
    server.routes[("POST", "/v2/checkout/orders")] = lambda r: httpx.Response(201, json=ONE_DOLLAR)
    server.routes[("GET", "/v2/checkout/orders/5O190127TN364715T")] = lambda r: httpx.Response(200, json=ONE_DOLLAR)
    server.routes[("GET", "/v1/customer/disputes")] = lambda r: httpx.Response(200, json={"items": []})
    return server


def test_everything_healthy_passes_and_shows_the_payee_ids():
    code, text = run(healthy_server())
    assert code == 0 and "FAIL" not in text and "All checks passed" in text
    assert "MERCHANT123" in text and "merchants.json" in text and "checkoutnow" in text
    assert "dry run" in text


def test_bad_keys_stop_early_with_a_helpful_hint():
    def refuse(request):
        return httpx.Response(401, json={"error": "invalid_client", "error_description": "Client Authentication failed"})

    client_http = httpx.Client(transport=httpx.MockTransport(refuse))
    from leash.paypal_api import PayPalClient
    out = io.StringIO()
    code = run_selftest(PayPalClient("CID", "BAD", http=client_http, sleep=lambda s: None), Console(file=out, width=200, color_system=None))
    text = out.getvalue()
    assert code == 1 and "FAIL" in text and "PAYPAL_CLIENT_SECRET" in text and "Create a $1.00" not in text


def test_a_disputes_permission_error_is_reported_but_other_checks_still_pass():
    server = healthy_server()
    server.routes[("GET", "/v1/customer/disputes")] = httpx.Response(403, json={"name": "NOT_AUTHORIZED", "message": "Authorization failed", "debug_id": "dbg9"})
    code, text = run(server)
    assert code == 1 and text.count("FAIL") == 1 and "read disputes" in text and "dbg9" in text and "Features" in text
    assert text.count("PASS") >= 5


def test_a_reply_without_a_payee_is_flagged_loudly():
    no_payee = {**ONE_DOLLAR, "purchase_units": [{k: v for k, v in ONE_DOLLAR["purchase_units"][0].items() if k != "payee"}]}
    server = healthy_server()
    server.routes[("GET", "/v2/checkout/orders/5O190127TN364715T")] = lambda r: httpx.Response(200, json=no_payee)
    code, text = run(server)
    assert code == 1 and "who gets paid" in text and "block every order" in text


def test_a_failed_order_creation_is_reported():
    server = Server()
    server.routes[("POST", "/v2/checkout/orders")] = httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY", "message": "bad", "debug_id": "d1",
                                                                              "details": [{"issue": "PAYEE_ACCOUNT_INVALID"}]})
    code, text = run(server)
    assert code == 1 and "PAYEE_ACCOUNT_INVALID" in text and "Read the order back" not in text
