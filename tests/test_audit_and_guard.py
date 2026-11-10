import json

from dwarpal.audit import AuditLog
from dwarpal.data_guard import filter_outbound
from dwarpal.guard import SessionGuard
from dwarpal.identity import KeyPair
from dwarpal.policy import Limits, Policy


def make_log(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl", KeyPair.generate())
    for i in range(5):
        log.append("agent_request", rid=f"r{i}", outcome="completed" if i % 2 else "denied")
    return log


def test_chain_verifies(tmp_path):
    v = make_log(tmp_path).verify()
    assert v["ok"] and v["count"] == 5 and len(v["head"]) == 64


def test_wrong_key_is_detected(tmp_path):
    make_log(tmp_path)
    impostor = AuditLog(tmp_path / "audit.jsonl", KeyPair.generate())
    assert not impostor.verify()["ok"]


def test_edit_is_detected(tmp_path):
    log = make_log(tmp_path)
    seq = log.tamper_for_demo()
    v = log.verify()
    assert not v["ok"] and v["broken_at"] == seq and "changed" in v["reason"]


def test_deletion_is_detected(tmp_path):
    log = make_log(tmp_path)
    lines = log.path.read_text().splitlines()
    del lines[2]
    log.path.write_text("\n".join(lines) + "\n")
    v = log.verify()
    assert not v["ok"] and "chain broken" in v["reason"]


def test_rehashing_without_key_is_detected(tmp_path):
    from dwarpal.audit import _record_hash

    log = make_log(tmp_path)
    rows = [json.loads(l) for l in log.path.read_text().splitlines()]
    rows[-1]["data"]["outcome"] = "forged"
    rows[-1]["hash"] = _record_hash(rows[-1])  # attacker recomputes the hash but cannot sign it
    log.path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    v = log.verify()
    assert not v["ok"] and "signature" in v["reason"]


def test_reopen_continues_chain(tmp_path):
    key = KeyPair.generate()
    AuditLog(tmp_path / "a.jsonl", key).append("x")
    log = AuditLog(tmp_path / "a.jsonl", key)
    log.append("y")
    assert log.verify()["ok"] and log.verify()["count"] == 2


def test_receipt_round_trip(tmp_path):
    log = make_log(tmp_path)
    claims = log.verify_receipt(log.issue_receipt({"intent": "reschedule_appointment"}))
    assert claims["intent"] == "reschedule_appointment" and claims["audit_head"]


def test_data_guard_drops_and_redacts():
    policy = Policy.load()
    clean, red = filter_outbound(
        {"status": "ok", "prescription": "Metformin", "nested": {"diagnosis": "x"},
         "message": "Call +91 9810012345 or riya@example.com"}, policy.data)
    assert "prescription" not in clean and "diagnosis" not in clean["nested"]
    assert "[redacted:phone_in]" in clean["message"] and "[redacted:email]" in clean["message"]
    assert len(red) == 4


def test_guard_loop_turn_and_probing():
    g = SessionGuard(Limits(max_turns_per_session=5, max_identical_requests=2, max_denials_per_session=2))
    s = g.session("s1", "a", "p")
    assert g.begin_turn(s, "x", {"a": 1}) is None
    assert g.begin_turn(s, "x", {"a": 1}) is None
    assert g.begin_turn(s, "x", {"a": 1}).code == "loop_detected"
    s2 = g.session("s2", "a", "p")
    g.record_denial(s2)
    g.record_denial(s2)
    assert g.begin_turn(s2, "y", {}).code == "probing"
    s3 = g.session("s3", "a", "p")
    codes = [g.begin_turn(s3, "z", {"i": i}) for i in range(6)]
    assert codes[-1].code == "turn_limit"


def test_rate_limit():
    g = SessionGuard(Limits(max_requests_per_minute=3))
    assert [g.check_rate("a", now=100 + i) is None for i in range(4)] == [True, True, True, False]
    assert g.check_rate("a", now=200) is None
