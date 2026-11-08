import json

import pytest

from leash.audit import AuditLog
from leash.cli import main
from leash.models import Mandate


@pytest.fixture
def demo_log(tmp_path):
    log = tmp_path / "audit.jsonl"
    assert main(["demo", "--log", str(log)]) == 0
    return log


def first_completed_request(log_path):
    for ev in AuditLog(log_path).events():
        if ev.type == "purchase_result" and ev.data["result"]["status"] == "completed":
            return ev.data["request_id"]


def test_attack_command_exit_code_and_output(capsys):
    assert main(["attack"]) == 0
    out = capsys.readouterr().out
    assert "attack suite" in out and "22 scenarios passed" in " ".join(out.split())


def test_demo_writes_a_log_and_an_example_packet(demo_log):
    assert AuditLog.verify_file(demo_log).ok
    assert (demo_log.parent / "evidence_example.md").read_text().startswith("# Payment evidence packet")


def test_verify_command_detects_tampering(demo_log, capsys):
    assert main(["verify", "--log", str(demo_log)]) == 0
    demo_log.write_text(demo_log.read_text().replace('"amount":"24.50"', '"amount":"2.50"', 1))
    assert main(["verify", "--log", str(demo_log)]) == 1
    assert "TAMPERING" in capsys.readouterr().out


def test_list_and_evidence_commands(demo_log, capsys, tmp_path):
    assert main(["list", "--log", str(demo_log)]) == 0
    assert "completed" in capsys.readouterr().out
    rid = first_completed_request(demo_log)
    out_file = tmp_path / "packet.md"
    assert main(["evidence", "--log", str(demo_log), "--request", rid, "--out", str(out_file)]) == 0
    assert "Payment evidence packet" in out_file.read_text()
    capsys.readouterr()  # forget everything printed so far
    assert main(["evidence", "--log", str(demo_log), "--request", rid, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["request_id"] == rid


def test_evidence_without_a_target_explains_itself(demo_log, capsys):
    assert main(["evidence", "--log", str(demo_log)]) == 2
    assert "--request" in capsys.readouterr().out
    assert main(["evidence", "--log", str(demo_log), "--request", "nope"]) == 2


def test_mandate_command_writes_rules_that_load_back(tmp_path):
    merchants = tmp_path / "merchants.json"
    merchants.write_text(json.dumps({"My Test Shop": ["sb-shop@business.example.com"]}))
    out = tmp_path / "mandate.json"
    code = main(["mandate", "Max $50 per order, $150 a week at My Test Shop", "--merchants", str(merchants),
                 "--owner", "Sam", "--out", str(out), "--yes"])
    assert code == 0
    m = Mandate.model_validate_json(out.read_text())
    assert m.owner == "Sam" and m.allowed_merchants == ("sb-shop@business.example.com",)


def test_mandate_command_refuses_unsafe_or_unknown(tmp_path, capsys):
    merchants = tmp_path / "merchants.json"
    merchants.write_text(json.dumps({"My Test Shop": ["sb-shop@business.example.com"]}))
    assert main(["mandate", "$99999 a week at My Test Shop", "--merchants", str(merchants), "--yes", "--out", str(tmp_path / "x.json")]) == 2
    assert not (tmp_path / "x.json").exists() and "safety" in capsys.readouterr().out


def test_sandbox_commands_without_keys_explain_what_to_do(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as info:
        main(["sandbox", "check"])
    assert "PAYPAL_CLIENT_ID" in str(info.value) and ".env.example" in str(info.value)


def test_missing_files_are_reported_not_crashed(tmp_path, capsys):
    with pytest.raises(SystemExit) as info:
        main(["mandate", "$100 a week at X", "--merchants", str(tmp_path / "none.json"), "--yes"])
    assert "none.json" in str(info.value)


def test_a_paypal_permission_refusal_when_creating_a_dispute_gives_hand_made_steps(monkeypatch, capsys):
    from leash import config
    from leash.paypal_api import PayPalError

    class Refusing:
        def create_sandbox_dispute(self, **kwargs):
            raise PayPalError("No permissions to set target_client_id", status=403)

    monkeypatch.setattr(config, "paypal_client", lambda: Refusing())
    code = main(["sandbox", "dispute", "--buyer-email", "b@personal.example.com", "--txn", "TX1", "--amount", "5.00"])
    out = " ".join(capsys.readouterr().out.split())
    assert code == 2 and "permission limit" in out and "Report a problem" in out and "sandbox disputes" in out


def test_other_dispute_errors_are_still_reported_normally(monkeypatch, capsys):
    from leash import config
    from leash.paypal_api import PayPalError

    class Broken:
        def create_sandbox_dispute(self, **kwargs):
            raise PayPalError("server on fire", status=500)

    monkeypatch.setattr(config, "paypal_client", lambda: Broken())
    assert main(["sandbox", "dispute", "--buyer-email", "b@x.y", "--txn", "T", "--amount", "5.00"]) == 2
    assert "server on fire" in capsys.readouterr().out
