import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from leash.audit import GENESIS, AuditError, AuditLog
from leash.clock import FakeClock


def make_log(path, n=4, key=None):
    log = AuditLog(path, key=key, clock=FakeClock())
    for i in range(n):
        log.append("step", {"n": i, "amount": Decimal("24.50"), "when": datetime(2026, 1, 1, tzinfo=timezone.utc)})
    return log


def lines_of(path):
    return path.read_text().splitlines()


def test_chain_verifies_and_head_moves():
    log = AuditLog(clock=FakeClock())
    assert log.head() == GENESIS and log.verify().ok
    h1 = log.append("a", {"x": 1}).hash
    h2 = log.append("b", {"x": 2}).hash
    assert h1 != h2 and log.head() == h2 and log.verify().count == 2


def test_reload_from_file_continues_the_chain(tmp_path):
    path = tmp_path / "a.jsonl"
    make_log(path)
    again = AuditLog(path, clock=FakeClock())
    assert again.verify().ok and again.verify().count == 4
    again.append("more", {"k": 1})
    assert AuditLog.verify_file(path).ok and AuditLog.verify_file(path).count == 5


def test_decimals_and_dates_hash_the_same_after_a_round_trip(tmp_path):
    path = tmp_path / "a.jsonl"
    make_log(path)
    assert AuditLog.verify_file(path).ok


@pytest.mark.parametrize("how", ["edit", "delete", "reorder", "duplicate", "insert_garbage_entry"])
def test_any_tampering_is_detected(tmp_path, how):
    path = tmp_path / "a.jsonl"
    make_log(path)
    ls = lines_of(path)
    if how == "edit":
        ls[2] = ls[2].replace('"n":2', '"n":9')
    elif how == "delete":
        del ls[1]
    elif how == "reorder":
        ls[1], ls[2] = ls[2], ls[1]
    elif how == "duplicate":
        ls.insert(2, ls[1])
    elif how == "insert_garbage_entry":
        fake = json.loads(ls[1])
        fake["data"] = {"n": 99}
        ls.insert(2, json.dumps(fake))
    path.write_text("\n".join(ls) + "\n")
    report = AuditLog.verify_file(path)
    assert not report.ok and report.broken_at is not None


def test_edit_is_pinpointed(tmp_path):
    path = tmp_path / "a.jsonl"
    make_log(path)
    ls = lines_of(path)
    ls[2] = ls[2].replace('"n":2', '"n":9')
    path.write_text("\n".join(ls) + "\n")
    assert AuditLog.verify_file(path).broken_at == 2


def _rebuild_unsigned(path):
    """An attacker with write access edits an entry and recomputes the whole chain with plain SHA-256."""
    events = [json.loads(line) for line in lines_of(path)]
    events[1]["data"]["n"] = 777
    prev = GENESIS
    out = []
    for ev in events:
        ev["prev_hash"] = prev
        ev["hash"] = AuditLog._digest(AuditLog._body(ev["seq"], ev["ts"], ev["type"], ev["data"], prev), None)
        prev = ev["hash"]
        out.append(json.dumps(ev, separators=(",", ":")))
    path.write_text("\n".join(out) + "\n")


def test_unsigned_log_can_be_rebuilt_by_a_determined_attacker(tmp_path):
    """Documented limitation: without a key the log is tamper-EVIDENT only against careless edits."""
    path = tmp_path / "a.jsonl"
    make_log(path)
    _rebuild_unsigned(path)
    assert AuditLog.verify_file(path).ok


def test_signed_log_resists_a_rebuild_without_the_key(tmp_path):
    path = tmp_path / "a.jsonl"
    make_log(path, key="s3cret")
    assert AuditLog.verify_file(path, key="s3cret").ok
    _rebuild_unsigned(path)
    report = AuditLog.verify_file(path, key="s3cret")
    assert not report.ok and report.signed


def test_wrong_or_missing_key_fails(tmp_path):
    path = tmp_path / "a.jsonl"
    make_log(path, key="right")
    assert not AuditLog.verify_file(path, key="wrong").ok
    assert not AuditLog.verify_file(path).ok


def test_tail_truncation_is_only_visible_through_the_head(tmp_path):
    """Documented limitation: cutting entries off the END leaves a valid chain. Publish head() to catch it."""
    path = tmp_path / "a.jsonl"
    log = make_log(path)
    head_before = log.head()
    path.write_text("\n".join(lines_of(path)[:-1]) + "\n")
    cut = AuditLog.verify_file(path)
    assert cut.ok and cut.count == 3 and cut.head != head_before


def test_corrupt_line_raises_a_clear_error(tmp_path):
    path = tmp_path / "a.jsonl"
    make_log(path)
    path.write_text(path.read_text() + "this is not json\n")
    with pytest.raises(AuditError, match="line 5"):
        AuditLog(path)


def test_a_ticking_clock_moves_on_its_own_and_a_frozen_one_does_not():
    ticking, frozen = FakeClock(tick=2), FakeClock()
    first, second = ticking(), ticking()
    assert (second - first).total_seconds() == 2 and frozen() == frozen()


def test_fsync_can_be_turned_off_without_changing_the_log(tmp_path):
    fast = tmp_path / "fast.jsonl"
    log = AuditLog(fast, clock=FakeClock(), fsync=False)
    log.append("a", {"x": 1})
    assert AuditLog.verify_file(fast).ok
