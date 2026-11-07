import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from leash.llm import FakeLLM
from leash.mandate_parser import Ceilings, MandateDraft, MandateError, build_mandate, parse_mandate, parse_with_rules

NOW = datetime(2026, 10, 21, 9, tzinfo=timezone.utc)
DIR = {"Pizza Palace": ["pizza-palace@example.com"], "Sushi Go": ["sushi-go@example.com", "MERCHSUSHI1"]}
SENTENCE = ("Keep team lunches under $200 a week at Pizza Palace and Sushi Go, max $60 per order, "
            "ask me before anything over $40, 2 orders a day, for 3 days.")


def ai(**fields):
    base = {"currency": "USD", "per_purchase_cap": "60", "budget_amount": "200", "budget_window_days": 7,
            "max_purchases_per_day": 3, "step_up_threshold": "40", "merchants": ["Pizza Palace"], "valid_days": 7}
    base.update(fields)
    return "Sure, here you go: " + json.dumps(base) + " Hope that helps!"


def test_rules_reader_understands_a_full_sentence():
    d = parse_with_rules(SENTENCE, DIR)
    assert (d.per_purchase_cap, d.budget_amount, d.budget_window_days) == (Decimal("60"), Decimal("200"), 7)
    assert (d.step_up_threshold, d.max_purchases_per_day, d.valid_days) == (Decimal("40"), 2, 3)
    assert d.merchants == ["Pizza Palace", "Sushi Go"]


@pytest.mark.parametrize("text,days,valid", [("$30 a day at Pizza Palace", 1, None), ("$300 a month at Pizza Palace", 30, None),
                                             ("$50 a week at Pizza Palace for 2 weeks", 7, 14)])
def test_windows_and_validity_units(text, days, valid):
    d = parse_with_rules(text, DIR)
    assert d.budget_window_days == days and d.valid_days == valid


def test_end_to_end_with_rules_builds_a_valid_mandate_and_lists_assumptions():
    m, notes, how = parse_mandate("Max $50 per order, $150 a week at Pizza Palace", DIR, owner="Sam", now=NOW)
    assert how == "rules" and m.per_purchase_cap == Decimal("50.00") and m.step_up_threshold == Decimal("25.00")
    assert m.allowed_merchants == ("pizza-palace@example.com",) and m.owner == "Sam"
    assert any("ask me first" in n for n in notes) and any("daily" in n for n in notes) and any("end date" in n for n in notes)


def test_directory_entry_with_several_ids_is_expanded():
    m, _, _ = parse_mandate("$100 a week at Sushi Go", DIR, owner="Sam", now=NOW)
    assert set(m.allowed_merchants) == {"sushi-go@example.com", "merchsushi1"}


def test_unknown_merchant_is_refused_not_guessed():
    with pytest.raises(MandateError, match="Blue Bottle"):
        build_mandate(MandateDraft(budget_amount=Decimal("100"), merchants=["Blue Bottle"]), DIR, owner="Sam", now=NOW)


def test_a_merchant_id_can_be_named_directly():
    m, _ = build_mandate(MandateDraft(budget_amount=Decimal("100"), merchants=["MERCHSUSHI1"]), DIR, owner="Sam", now=NOW)
    assert m.allowed_merchants == ("merchsushi1",)


@pytest.mark.parametrize("draft,msg", [
    (MandateDraft(merchants=["Pizza Palace"]), "total"),
    (MandateDraft(budget_amount=Decimal("100")), "merchant"),
    (MandateDraft(budget_amount=Decimal("-5"), merchants=["Pizza Palace"]), "not valid"),
])
def test_incomplete_or_invalid_drafts_are_refused(draft, msg):
    with pytest.raises(MandateError, match=msg):
        build_mandate(draft, DIR, owner="Sam", now=NOW)


@pytest.mark.parametrize("fields", [dict(budget_amount="5000"), dict(per_purchase_cap="900", budget_amount="1000"),
                                    dict(max_purchases_per_day=99), dict(valid_days=400), dict(budget_window_days=90)])
def test_ceilings_cannot_be_exceeded_even_by_the_ai(fields):
    with pytest.raises(MandateError, match="safety"):
        parse_mandate("anything", DIR, owner="Sam", now=NOW, llm=FakeLLM([ai(**fields)]))


def test_ceilings_can_be_raised_in_code_by_the_owner():
    m, _, _ = parse_mandate("x", DIR, owner="Sam", now=NOW, llm=FakeLLM([ai(budget_amount="5000", per_purchase_cap="900", step_up_threshold="100")]),
                            ceilings=Ceilings(per_purchase=Decimal("1000"), budget=Decimal("10000")))
    assert m.budget_amount == Decimal("5000.00")


def test_ai_answer_is_used_when_valid():
    llm = FakeLLM([ai(merchants=["Pizza Palace", "Sushi Go"])])
    m, _, how = parse_mandate("whatever", DIR, owner="Sam", now=NOW, llm=llm)
    assert how == "ai" and len(m.allowed_merchants) == 3 and llm.calls and "whatever" in llm.calls[0][1]


def test_unusable_ai_answer_falls_back_to_the_rule_reader():
    for reply in ("I am sorry, I cannot do that.", "{not json}", '{"budget_amount": "abc"}', "[1, 2]"):
        m, _, how = parse_mandate(SENTENCE, DIR, owner="Sam", now=NOW, llm=FakeLLM([reply]))
        assert how == "rules" and m.budget_amount == Decimal("200.00")


def test_ai_cannot_invent_a_merchant():
    with pytest.raises(MandateError, match="Evil Corp"):
        parse_mandate("x", DIR, owner="Sam", now=NOW, llm=FakeLLM([ai(merchants=["Evil Corp"])]))


def test_ai_nulls_become_assumptions_and_step_up_is_clipped_to_the_cap():
    m, notes, _ = parse_mandate("x", DIR, owner="Sam", now=NOW,
                                llm=FakeLLM([ai(per_purchase_cap="30", step_up_threshold="90", max_purchases_per_day=None, valid_days=None)]))
    assert m.step_up_threshold == m.per_purchase_cap == Decimal("30.00") and len(notes) == 2


class BrokenAI:
    def complete(self, *args, **kwargs):
        raise ConnectionError("no internet")


def test_a_broken_ai_falls_back_to_the_rule_reader_and_says_so():
    m, notes, how = parse_mandate(SENTENCE, DIR, owner="Sam", now=NOW, llm=BrokenAI())
    assert how == "rules" and m.budget_amount == Decimal("200.00")
    assert notes[0].startswith("The AI could not be used (ConnectionError)")
