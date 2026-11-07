from leash import views
from leash.scenarios import EVIL, PIZZA, SUSHI


def cards_of(world):
    return views.request_cards(world.audit.events())


def test_a_completed_purchase_card(world):
    r = world.pay(world.order(PIZZA, "24.50", description="Lunch"), claimed_merchant=PIZZA, claimed_amount="24.50", agent_id="bot")
    c = cards_of(world)[0]
    assert c["request_id"] == r.request_id and c["status"] == "completed" and c["agent_id"] == "bot"
    assert c["paypal_said"]["amount"] == "24.50" and PIZZA in c["paypal_said"]["payees"]
    assert c["capture_id"] == r.capture_id and not c["mismatch"] and c["reason_labels"] == ["Inside all of your rules"]


def test_a_tricked_agent_is_flagged_as_a_mismatch(world):
    world.pay(world.order(EVIL, "48.00"), claimed_merchant=PIZZA)
    c = cards_of(world)[0]
    assert c["status"] == "blocked" and c["mismatch"]
    assert "The agent's story does not match PayPal's record" in c["reason_labels"]
    assert "This merchant is not on your list" in c["reason_labels"]


def test_an_amount_lie_is_a_mismatch_even_with_the_right_merchant(world):
    world.pay(world.order(PIZZA, "50.00"), claimed_merchant=PIZZA, claimed_amount="5.00")
    assert cards_of(world)[0]["mismatch"]


def test_cards_are_newest_first_and_limited(world):
    for i in range(5):
        world.pay(world.order(PIZZA if i % 2 else SUSHI, f"{10 + i}.00"))
    cards = cards_of(world)
    assert [c["paypal_said"]["amount"] for c in cards][:2] == ["14.00", "13.00"]
    assert len(views.request_cards(world.audit.events(), limit=2)) == 2


def test_waiting_and_approved_states(world):
    r = world.pay(world.order(SUSHI, "45.00"))
    assert cards_of(world)[0]["status"] == "needs_owner_approval"
    world.guard.approve(r.request_id)
    c = cards_of(world)[0]
    assert c["status"] == "completed" and c["approved_flags"] == ["needs_owner_approval"]


def test_malformed_requests_get_a_card_too(world):
    world.guard.pay({"order_id": "../x"})
    c = cards_of(world)[0]
    assert c["status"] == "blocked" and c["reason_labels"] == ["The request was malformed"]


def test_untrusted_text_is_passed_through_untouched_for_the_page_to_escape(world):
    nasty = '<img src=x onerror="alert(1)">'
    world.pay(world.order(PIZZA, "20.00"), description=nasty)
    assert cards_of(world)[0]["description"] == nasty  # escaping is the page's job (and a test checks it)


def test_event_rows(world):
    world.pay(world.order(PIZZA, "20.00"))
    rows = views.event_rows(world.audit.events())
    assert rows[0]["type"] == "mandate_set" and all(len(r["hash"]) == 12 for r in rows) and rows[-1]["seq"] == len(rows) - 1


def test_every_reason_code_has_a_plain_label():
    from leash.models import Reason
    assert all(r.value in views.REASON_LABELS for r in Reason)
