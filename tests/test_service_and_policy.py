"""No privileged path: the AI agent and the human desk write through the same validated service."""

import inspect
import time

import pytest

import dwarpal.gateway as gateway_module
from dwarpal.business.service import Actor, ClinicService
from dwarpal.escalation import EscalationQueue
from dwarpal.messages import customer_message
from dwarpal.policy import Policy
from dwarpal.stepup import StepUpService

AGENT = Actor("agent", "agent:riya-assistant", "P-1001")
STAFF = Actor("staff", "desk:priya")


@pytest.fixture
def svc():
    writes = []
    s = ClinicService(on_write=writes.append)
    s.writes = writes
    return s


def test_same_rules_for_agent_and_staff(svc):
    # Neither channel can refund more than was paid, or refund a booked visit.
    for actor in (AGENT, STAFF):
        assert svc.refund_copay(actor, "APT-488", 5000)["status"] == "rejected"
        assert svc.refund_copay(actor, "APT-501", 100)["status"] == "rejected"
        assert svc.reschedule(actor, "APT-501", "2030-01-01T09:00")["status"] == "rejected"


def test_agent_only_sees_its_principal(svc):
    assert svc.get_appointment(AGENT, "APT-777")["status"] == "not_found"
    assert svc.get_appointment(AGENT, "APT-000")["status"] == "not_found"  # same answer: no probing
    assert svc.get_appointment(STAFF, "APT-777")["status"] == "ok"


def test_clinical_records_never_go_to_agents(svc):
    assert svc.get_clinical_record(AGENT, "P-1001")["status"] == "rejected"
    assert svc.get_clinical_record(STAFF, "P-1001")["status"] == "ok"


def test_every_write_is_recorded_with_its_channel(svc):
    svc.reschedule(AGENT, "APT-501", "2026-11-13T09:30")
    svc.cancel(STAFF, "APT-777", "patient called")
    assert [(w["action"], w["channel"]) for w in svc.writes] == [("reschedule", "agent"), ("cancel", "staff")]


def test_gateway_has_no_path_to_the_service():
    src = inspect.getsource(gateway_module)
    assert "ClinicService" not in src and "business.service" not in src


def test_policy_tiers_and_validation():
    p = Policy.load()
    tiers = {name: r.tier for name, r in p.intents.items()}
    assert tiers["clinic_info"] == "open" and tiers["cancel_appointment"] == "binding"
    assert tiers["billing_dispute"] == "human"
    with pytest.raises(ValueError):
        Policy.from_dict({"business": {"id": "x"}, "intents": {"pay": {"tier": "binding"}}})  # no scope
    with pytest.raises(ValueError):
        Policy.from_dict({"business": {"id": "x"}, "intents": {"pay": {"tier": "vip", "scope": "a:b"}}})


def test_v01_policies_still_load():
    p = Policy.from_dict({"business": {"id": "x"}, "intents": {
        "a": {"scope": "s:r", "mode": "a2a"}, "b": {"scope": "s:w", "mode": "human"}}})
    assert p.intents["a"].tier == "account" and p.intents["b"].tier == "human"


def test_customer_message_fallbacks():
    p = Policy.load()
    assert "approval" in customer_message(p, "step_up_required", intent="cancel_appointment", reason="step_up_required",
                                          fields={"action_summary": "cancel appointment APT-501"})
    # unknown placeholders are dropped, never shown as {braces}
    msg = customer_message(p, "escalated", intent="billing_dispute", reason="human_only", fields={})
    assert "{" not in msg


def test_step_up_bound_to_exact_action():
    su = StepUpService(Policy.load().step_up)
    c = su.create(agent_id="a", agent_name="A", principal_id="P", principal_name="Pat", intent="cancel_appointment",
                  params={"appointment_id": "APT-1"}, summary="cancel")
    su.approve(c.id, ["app_biometric"])
    assert su.check(c.id, principal_id="P", agent_id="a", intent="cancel_appointment",
                    params={"appointment_id": "APT-2"})[0] == "mismatch"
    assert su.check(c.id, principal_id="P", agent_id="other", intent="cancel_appointment",
                    params={"appointment_id": "APT-1"})[0] == "mismatch"
    assert su.check(c.id, principal_id="P", agent_id="a", intent="cancel_appointment",
                    params={"appointment_id": "APT-1"})[0] == "ok"


def test_step_up_expires():
    su = StepUpService(Policy.load().step_up)
    c = su.create(agent_id="a", agent_name="A", principal_id="P", principal_name="Pat", intent="x", params={},
                  summary="x", now=time.time() - 1000)
    su.approve(c.id, ["app_biometric"])
    assert su.check(c.id, principal_id="P", agent_id="a", intent="x", params={})[0] == "expired"


def test_two_weak_factors_make_one_strong():
    su = StepUpService(Policy.load().step_up)
    c = su.create(agent_id="a", agent_name="A", principal_id="P", principal_name="Pat", intent="x", params={},
                  summary="x")
    assert su.approve(c.id, ["keypad_pin"])[0].status == "pending"
    assert su.approve(c.id, ["sms_code"])[0].status == "approved"  # no speech needed: accessible path


def test_join_codes_are_one_time_and_expire():
    q = EscalationQueue()
    t = q.open(response_hours=24, join_ttl_hours=1, session_id="s", agent_id="a", agent_name="A",
               principal={"id": "P", "name": "Pat"}, intent="x", params={}, reason_code="r", reason="r", risk="high")
    assert q.join(t.join_code.lower())[0] == "ok"
    assert q.join(t.join_code)[0] == "used"
    t2 = q.open(response_hours=24, join_ttl_hours=1, session_id="s", agent_id="a", agent_name="A",
                principal={"id": "P", "name": "Pat"}, intent="x", params={}, reason_code="r", reason="r", risk="high")
    assert q.join(t2.join_code, now=time.time() + 7200)[0] == "expired"
    assert q.join("NOPE00")[0] == "unknown"
