import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from leash.demo_world import SCENARIOS
from leash.web import STATIC, create_app, make_demo_world, make_sandbox_world
from tests.test_paypal_client import ORDER, Server, make


@pytest.fixture
def world():
    return make_demo_world()


@pytest.fixture
def client(world):
    return TestClient(create_app(world), base_url="http://localhost")


def post(client, world, path, json=None):
    return client.post(path, json=json if json is not None else {}, headers={"X-Leash-Token": world.token})


def card(client, request_id):
    return next(c for c in client.get("/api/state").json()["cards"] if c["request_id"] == request_id)


# ------------------------------------------------------------------ protections
@pytest.mark.parametrize("host", ["evil.example.com", "localhost.evil.com", "127.0.0.1.evil.com", "localhost:80@evil.com",
                                  "evil.com:8765", "localhost:99999x", "", "0.0.0.0:8765"])
def test_other_hosts_are_refused(client, host):
    r = client.get("/api/state", headers={"host": host})
    assert r.status_code == 400


@pytest.mark.parametrize("host", ["localhost", "localhost:8765", "127.0.0.1:8765", "[::1]:8765"])
def test_localhost_hosts_are_accepted(client, host):
    assert client.get("/api/state", headers={"host": host}).status_code == 200


def test_every_post_needs_the_token(client, world):
    for path in ("/api/kill", "/api/release", "/api/pay", "/api/orders", "/api/approve/abc", "/api/demo/lunch",
                 "/api/demo/reset", "/api/mandate/preview", "/api/mandate/confirm"):
        assert client.post(path, json={}).status_code == 403, path
        assert client.post(path, json={}, headers={"X-Leash-Token": "wrong"}).status_code == 403, path
    assert post(client, world, "/api/kill").status_code == 200


def test_oversized_bodies_are_refused(client, world):
    r = client.post("/api/pay", content=b"{" + b" " * 70000 + b"}", headers={"X-Leash-Token": world.token, "Content-Type": "application/json"})
    assert r.status_code == 413


def test_security_headers_are_on_every_response(client):
    for path in ("/", "/api/state", "/static/dashboard.js", "/nope"):
        h = client.get(path).headers
        assert "script-src 'self'" in h["content-security-policy"] and "unsafe-inline'" not in h["content-security-policy"].split("script-src")[1].split(";")[0]
        assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff" and h["cache-control"] == "no-store"


def test_the_page_carries_the_token_and_only_known_static_files_are_served(client, world):
    page = client.get("/")
    assert f'content="{world.token}"' in page.text and "__TOKEN__" not in page.text
    assert client.get("/static/dashboard.js").status_code == 200 and client.get("/static/dashboard.css").status_code == 200
    assert client.get("/static/vendor/ag-grid-community.min.js").status_code == 200
    for bad in ("/static/../web.py", "/static/index.html", "/static/%2e%2e/web.py", "/static/vendor/other.js"):
        assert client.get(bad).status_code == 404, bad


def test_the_token_is_different_every_run():
    assert make_demo_world().token != make_demo_world().token


# ------------------------------------------------------------------ reading state
def test_initial_state(client):
    s = client.get("/api/state").json()
    assert s["mode"] == "demo" and not s["kill_switch"] and s["cards"] == [] and s["chain"]["ok"]
    assert s["mandate"]["owner"] == "Sam" and len(s["mandate_lines"]) == 7
    assert s["budget"]["spent"] == "0" and s["budget"]["total"] == "400.00" and s["budget"]["max_per_day"] == 10
    assert [x["id"] for x in s["scenarios"]] == [x["id"] for x in SCENARIOS]


def test_amounts_are_strings_never_floats(client, world):
    run(client, world, "lunch")
    s = client.get("/api/state").json()
    assert s["budget"]["spent"] == "24.50" and s["cards"][0]["paypal_said"]["amount"] == "24.50"
    assert all(isinstance(s["budget"][k], str) for k in ("total", "spent", "remaining"))


# ------------------------------------------------------------------ demo scenarios through the API
def run(client, world, name):
    r = post(client, world, f"/api/demo/{name}")
    assert r.status_code == 200, r.text
    return r.json()


def test_lunch_then_blocked_tricks(client, world):
    out = run(client, world, "lunch")
    assert card(client, out["request_id"])["status"] == "completed"
    for name in ("injection", "lookalike"):
        assert card(client, run(client, world, name)["request_id"])["status"] == "blocked"
    assert client.get("/api/state").json()["budget"]["spent"] == "24.50"  # only the lunch moved money


def test_big_purchase_can_be_approved_or_rejected_from_the_dashboard(client, world):
    first = run(client, world, "big")["request_id"]
    c = card(client, first)
    assert c["status"] == "needs_owner_approval" and c["can_decide"]
    assert post(client, world, f"/api/approve/{first}").json()["status"] == "completed"
    assert card(client, first)["can_decide"] is False
    second = run(client, world, "big")["request_id"]
    assert post(client, world, f"/api/reject/{second}", {"reason": "no thanks"}).json()["status"] == "rejected_by_owner"


def test_approving_something_that_is_not_waiting_is_a_clear_error(client, world):
    r = post(client, world, "/api/approve/doesnotexist")
    assert r.status_code == 409 and "Nothing is waiting" in r.json()["error"]


def test_price_change_is_blocked_at_capture(client, world):
    out = run(client, world, "price_change")
    c = card(client, out["request_id"])
    assert c["status"] == "blocked" and "The order changed after you saw it, so nothing was charged" in c["reason_labels"]


def test_replay_charges_once(client, world):
    out = run(client, world, "replay")
    assert "completed" in out["lines"][0] and "blocked" in out["lines"][1]
    spent = float(client.get("/api/state").json()["budget"]["spent"])
    assert spent < 30  # one lunch, not two


def test_rogue_agent_trips_the_lockout_and_the_owner_can_release_it(client, world):
    run(client, world, "rogue")
    assert client.get("/api/state").json()["kill_switch"] is True
    assert card(client, run(client, world, "lunch")["request_id"])["status"] == "blocked"
    assert post(client, world, "/api/release").json() == {"kill_switch": False}
    assert card(client, run(client, world, "lunch")["request_id"])["status"] == "completed"


def test_kill_switch_endpoints(client, world):
    assert post(client, world, "/api/kill").json() == {"kill_switch": True}
    assert client.get("/api/state").json()["kill_switch"] is True
    assert post(client, world, "/api/release").json() == {"kill_switch": False}


def test_dispute_and_evidence(client, world):
    assert "Nothing has been paid yet" in run(client, world, "dispute")["lines"][0]
    paid = run(client, world, "lunch")["request_id"]
    out = run(client, world, "dispute")
    assert out["request_id"] == paid and out["dispute_id"].startswith("PP-D-MOCK")
    r = client.get(f"/api/evidence/{paid}", params={"dispute_id": out["dispute_id"]})
    assert r.status_code == 200 and "The dispute" in r.json()["markdown"] and out["dispute_id"] in r.json()["markdown"]
    assert r.json()["filename"].startswith("leash-evidence-") and r.json()["json"]["facts"]["final_status"] == "completed"
    assert client.get("/api/evidence/nope").status_code == 404


def test_tamper_demo_catches_the_copy_and_leaves_the_real_log_alone(client, world):
    assert "nothing to tamper" in run(client, world, "tamper")["lines"][0].lower()
    run(client, world, "lunch")
    lines = run(client, world, "tamper")["lines"]
    assert any("TAMPERING FOUND" in l for l in lines) and any("untouched: intact" in l for l in lines)
    assert client.get("/api/verify").json()["ok"] is True


def test_unknown_scenario_and_reset(client, world):
    assert post(client, world, "/api/demo/nonsense").status_code == 404
    run(client, world, "lunch")
    assert post(client, world, "/api/demo/reset").json() == {"ok": True}
    s = client.get("/api/state").json()
    assert s["cards"] == [] and s["chain"]["count"] == 1  # just the new world's rules entry
    run(client, world, "lunch")  # and it still works


def test_events_endpoint(client, world):
    run(client, world, "lunch")
    rows = client.get("/api/events").json()
    assert rows[0]["type"] == "mandate_set" and rows[-1]["type"] == "purchase_result" and set(rows[0]) == {"seq", "ts", "type", "summary", "hash"}


# ------------------------------------------------------------------ agent door and merchant desk
def test_pay_endpoint_accepts_garbage_and_logs_it(client, world):
    for body in ({}, {"order_id": "../../etc"}, {"order_id": "ORDGOOD123", "claimed_amount": "-5"}):
        r = post(client, world, "/api/pay", body)
        assert r.status_code == 200 and r.json()["status"] == "blocked"
    assert client.get("/api/state").json()["budget"]["spent"] == "0"
    assert post(client, world, "/api/pay", None).status_code in (200, 422)
    assert client.post("/api/pay", content=b"[1,2,3]", headers={"X-Leash-Token": world.token, "Content-Type": "application/json"}).status_code == 422


def test_merchant_desk_creates_orders_and_the_agent_pays_them(client, world):
    o = post(client, world, "/api/orders", {"amount": "24.50", "payee": "pizza-palace@example.com", "description": "Lunch"}).json()
    assert o["amount"] == "24.50" and o["status"] == "PAYER_ACTION_REQUIRED"
    r = post(client, world, "/api/pay", {"order_id": o["order_id"], "claimed_amount": "24.50"}).json()
    assert r["status"] == "completed"
    for bad in ({"amount": "-1"}, {"amount": "abc"}, {"amount": "1.234"}, {"amount": "5", "currency": "USDD"}):
        assert post(client, world, "/api/orders", bad).status_code in (400, 422), bad


# ------------------------------------------------------------------ editing the rules
def test_rules_preview_then_confirm_activates_exactly_what_was_shown(client, world):
    p = post(client, world, "/api/mandate/preview", {"text": "Max $20 per order, $60 a week at Pizza Palace"}).json()
    assert p["how"] == "rules" and any("$20.00" in l for l in p["lines"]) and p["preview_id"]
    assert client.get("/api/state").json()["mandate"]["per_purchase_cap"] == "100.00"  # nothing changed yet
    assert post(client, world, "/api/mandate/confirm", {"preview_id": p["preview_id"]}).status_code == 200
    s = client.get("/api/state").json()
    assert s["mandate"]["per_purchase_cap"] == "20.00" and s["mandate"]["allowed_merchants"] == ["pizza-palace@example.com"]
    assert post(client, world, "/api/mandate/confirm", {"preview_id": p["preview_id"]}).status_code == 404  # single use


@pytest.mark.parametrize("text,fragment", [("$99999 a week at Pizza Palace", "safety"),
                                           ("$100 a week at Blue Bottle", "Pizza Palace, Sushi Go"),  # lists the merchants you do have
                                           ("hello there", "merchant"), ("Pizza Palace please", "total")])
def test_unsafe_or_unknown_rules_are_refused(client, world, text, fragment):
    r = post(client, world, "/api/mandate/preview", {"text": text})
    assert r.status_code == 400 and fragment in r.json()["error"]


def test_confirming_an_unknown_preview_fails(client, world):
    assert post(client, world, "/api/mandate/confirm", {"preview_id": "x" * 12}).status_code == 404
    assert post(client, world, "/api/mandate/confirm", {"preview_id": "short"}).status_code == 422


# ------------------------------------------------------------------ sandbox mode (with a faked PayPal server)
def test_sandbox_mode_flow_with_a_fake_paypal(tmp_path):
    server = Server()
    server.routes[("POST", "/v2/checkout/orders")] = lambda r: httpx.Response(201, json=ORDER)
    server.routes[("GET", "/v2/checkout/orders/5O190127TN364715T")] = lambda r: httpx.Response(200, json=ORDER)
    world = make_sandbox_world(make(server), tmp_path / "a.jsonl", directory={"My Shop": ["MERCHANT123"]})
    c = TestClient(create_app(world), base_url="http://localhost")
    s = c.get("/api/state").json()
    assert s["mode"] == "sandbox" and s["mandate"] is None and s["scenarios"] == []
    assert post(c, world, "/api/demo/lunch").status_code == 404 and post(c, world, "/api/demo/reset").status_code == 404
    assert post(c, world, "/api/pay", {"order_id": "5O190127TN364715T"}).json()["status"] == "blocked"  # no rules yet
    p = post(c, world, "/api/mandate/preview", {"text": "Max $50 per order, $150 a week at My Shop"}).json()
    post(c, world, "/api/mandate/confirm", {"preview_id": p["preview_id"]})
    o = post(c, world, "/api/orders", {"amount": "24.50"}).json()
    assert o["order_id"] == "5O190127TN364715T" and "MERCHANT123" in o["payee_ids"]
    paid = post(c, world, "/api/pay", {"order_id": o["order_id"], "claimed_amount": "24.50"}).json()
    assert paid["status"] == "awaiting_paypal_checkout_approval" and paid["approval_url"].startswith("https://www.sandbox.paypal.com/checkoutnow")
    card_ = next(x for x in c.get("/api/state").json()["cards"] if x["request_id"] == paid["request_id"])
    assert card_["can_finalize"] and card_["approval_url"] == paid["approval_url"]


def test_paypal_failures_become_clear_502s(tmp_path):
    server = Server()
    server.routes[("POST", "/v2/checkout/orders")] = httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY", "message": "bad payee", "debug_id": "d"})
    world = make_sandbox_world(make(server), tmp_path / "a.jsonl")
    c = TestClient(create_app(world), base_url="http://localhost")
    r = post(c, world, "/api/orders", {"amount": "5"})
    assert r.status_code == 502 and "PayPal said" in r.json()["error"]


# ------------------------------------------------------------------ the page's own code
def test_the_javascript_never_builds_html_from_strings():
    js = (STATIC / "dashboard.js").read_text(encoding="utf-8")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "setAttribute('on", "srcdoc"):
        assert sink not in js, sink


def test_the_page_loads_no_code_from_the_internet():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    sources = re.findall(r'(?:src|href)="([^"]+)"', html)
    assert sources and all(s.startswith("/static/") for s in sources), sources
    assert "<script>" not in html and "onclick=" not in html  # no inline scripts or handlers (the CSP forbids them)


def test_the_bundled_grid_library_has_its_license():
    vendor = Path(STATIC / "vendor")
    assert (vendor / "ag-grid-community.min.js").stat().st_size > 1_000_000
    assert "MIT" in (vendor / "ag-grid-community.LICENSE.txt").read_text()


def test_price_change_while_waiting_is_blocked_when_the_owner_presses_approve(client, world):
    out = run(client, world, "price_change_wait")
    c = card(client, out["request_id"])
    assert c["status"] == "needs_owner_approval" and c["can_decide"] and c["paypal_said"]["amount"] == "85.00"
    r = post(client, world, f"/api/approve/{out['request_id']}").json()
    assert r["status"] == "blocked" and "85.00" in r["detail"] and "99.00" in r["detail"]
    assert card(client, out["request_id"])["status"] == "blocked"
    assert client.get("/api/state").json()["budget"]["spent"] == "0"


def test_the_price_change_demos_work_in_any_order_after_other_purchases(client, world):
    """Regression for the first real run: earlier Sushi purchases made the demo hold the order and then crash."""
    for name in ("big", "big", "lunch"):
        r = run(client, world, name)
        if card(client, r["request_id"])["can_decide"]:
            post(client, world, f"/api/approve/{r['request_id']}")
    out = run(client, world, "price_change")
    assert card(client, out["request_id"])["status"] == "blocked"
    assert "Authorised at 50.00" in out["lines"][0] and "now says 95.00" in out["lines"][1] and "Nothing was charged" in out["lines"][1]
    run(client, world, "price_change_wait")
    run(client, world, "price_change")  # and again, no crash
