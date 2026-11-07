from leash import policy
from leash.models import Reason, Verdict
from leash.scenarios import SCENARIOS, run_all


def test_every_attack_scenario_passes():
    outcomes = run_all()
    assert len(outcomes) == len(SCENARIOS) == 22
    assert [o.name for o in outcomes if not o.passed] == []


def test_the_suite_notices_when_the_rules_engine_is_sabotaged(monkeypatch):
    """If this test ever passes with a broken rules engine, the attack suite is worthless."""
    real = policy.evaluate

    def always_allow(*args, **kwargs):
        decision = real(*args, **kwargs)
        return decision.model_copy(update={"verdict": Verdict.ALLOW, "reasons": [Reason.OK], "messages": ["sabotaged"]})

    monkeypatch.setattr(policy, "evaluate", always_allow)
    failed = [o.name for o in run_all() if not o.passed]
    assert {"over_cap", "prompt_injection", "lookalike", "weekly_budget", "runaway_loop", "kill_switch", "expired"} <= set(failed)


def test_money_moved_is_reported_honestly():
    by_name = {o.name: o for o in run_all()}
    assert str(by_name["normal_lunch"].money_moved) == "24.50"
    assert all(by_name[n].money_moved == 0 for n in ("over_cap", "prompt_injection", "lookalike", "bait_and_switch", "change_while_waiting", "garbage_input"))
