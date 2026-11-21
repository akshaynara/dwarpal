"""The clinic's business service: the one place appointments and money change.

No privileged path. The AI front-desk agent's tools and the human front desk both call this
service, and every call is validated the same way whoever makes it. The gateway holds no
reference to it at all: it can only ask the business agent, which can only ask the service.

In a real deployment this is the business's existing API (scheduling system, billing system),
and Dwarpal integrates with it rather than replacing it.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

SEED: dict[str, Any] = {
    "patients": {
        "P-1001": {"name": "Riya Sharma", "phone": "+91 98100 12345", "dob": "1991-04-18",
                   "diagnosis": "Type 2 diabetes", "prescription": "Metformin 500mg BD",
                   "lab_results": "HbA1c 7.1% (Oct 2026)", "insurance_id": "HW-88231"},
        "P-2002": {"name": "Vikram Rao", "phone": "+91 99000 54321", "dob": "1984-09-02",
                   "diagnosis": "Hypertension", "prescription": "Amlodipine 5mg OD",
                   "lab_results": "BP 138/88", "insurance_id": "HW-11902"},
    },
    "appointments": {
        "APT-501": {"patient_id": "P-1001", "doctor": "Dr. Mehta", "slot": "2026-11-12T10:00",
                    "status": "booked", "copay_inr": 800, "copay_refunded": False},
        "APT-488": {"patient_id": "P-1001", "doctor": "Dr. Mehta", "slot": "2026-11-05T11:30",
                    "status": "cancelled", "copay_inr": 800, "copay_refunded": False},
        "APT-777": {"patient_id": "P-2002", "doctor": "Dr. Rao", "slot": "2026-11-13T15:00",
                    "status": "booked", "copay_inr": 600, "copay_refunded": False},
    },
    "open_slots": {
        "Dr. Mehta": ["2026-11-13T09:30", "2026-11-13T11:00", "2026-11-14T10:00", "2026-11-14T12:30"],
        "Dr. Rao": ["2026-11-14T09:00", "2026-11-15T14:00"],
    },
    "info": {
        "hours": "Monday to Saturday, 8 am to 8 pm. Closed on Sundays and public holidays.",
        "address": "Sunrise Family Clinic, Sector 44, Gurugram.",
        "insurers": "We accept HealthWallet, Star Health, HDFC Ergo and Niva Bupa cashless cards.",
    },
}


def human_slot(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%a %d %b at %H:%M").replace(" 0", " ")
    except (TypeError, ValueError):
        return str(iso)


def human_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%a %d %b").replace(" 0", " ")
    except (TypeError, ValueError):
        return str(iso)


@dataclass(frozen=True)
class Actor:
    """Who is asking the service to act."""
    channel: str  # "agent" (an AI caller via the gateway) or "staff" (a person at the front desk)
    id: str
    principal_id: str | None = None  # the patient the caller acts for, if any


class ClinicService:
    def __init__(self, on_write: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.on_write = on_write
        self.reset()

    def reset(self) -> None:
        self.data = copy.deepcopy(SEED)
        self.changes: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ helpers

    def _record(self, actor: Actor, action: str, **details: Any) -> None:
        change = {"ts": round(time.time(), 3), "action": action, "channel": actor.channel, "actor": actor.id,
                  "principal": actor.principal_id, **details}
        self.changes.append(change)
        if self.on_write:
            self.on_write(change)

    def _owned(self, actor: Actor, appointment_id: str) -> tuple[dict | None, dict | None]:
        apt = self.data["appointments"].get(appointment_id)
        # Staff can see any appointment; anyone else only their principal's.
        # "Doesn't exist" and "not yours" get the same answer, so callers can't probe which ids exist.
        if not apt or (actor.channel != "staff" and apt["patient_id"] != actor.principal_id):
            return None, {"status": "not_found",
                          "message": f"There's no appointment {appointment_id} for this patient."}
        return apt, None

    # ------------------------------------------------------------------ reads

    def clinic_info(self, actor: Actor, topic: str = "hours") -> dict:
        info = self.data["info"]
        key = next((k for k in info if k in str(topic).lower()), "hours")
        return {"status": "ok", "topic": key, "answer": info[key], "message": info[key]}

    def list_open_slots(self, actor: Actor, doctor: str, date: str) -> dict:
        slots = [s for s in self.data["open_slots"].get(doctor, []) if s.startswith(str(date))]
        times = ", ".join(s[11:] for s in slots) or "none"
        return {"status": "ok", "doctor": doctor, "date": date, "slots": slots, "slot_count": len(slots),
                "slot_times": times, "date_human": human_date(date),
                "message": f"{doctor} has {len(slots)} open slot(s) on {date}: {times}."}

    def get_appointment(self, actor: Actor, appointment_id: str) -> dict:
        apt, err = self._owned(actor, appointment_id)
        if err:
            return err
        return {"status": "ok", "appointment_id": appointment_id, "doctor": apt["doctor"], "slot": apt["slot"],
                "slot_human": human_slot(apt["slot"]), "state": apt["status"],
                "message": f"{appointment_id} with {apt['doctor']} on {human_slot(apt['slot'])} is {apt['status']}."}

    def get_clinical_record(self, actor: Actor, patient_id: str) -> dict:
        """Clinical data never goes to an AI caller, whatever the agent was told."""
        if actor.channel != "staff":
            return {"status": "rejected", "message": "Clinical records are only released by clinic staff."}
        p = self.data["patients"].get(patient_id)
        if not p:
            return {"status": "not_found", "message": "No such patient."}
        self._record(actor, "read_clinical_record", patient=patient_id)
        return {"status": "ok", "diagnosis": p["diagnosis"], "prescription": p["prescription"],
                "lab_results": p["lab_results"], "message": "Clinical record retrieved."}

    # ------------------------------------------------------------------ writes

    def reschedule(self, actor: Actor, appointment_id: str, new_slot: str) -> dict:
        apt, err = self._owned(actor, appointment_id)
        if err:
            return err
        if apt["status"] != "booked":
            return {"status": "rejected", "message": f"{appointment_id} is {apt['status']}, so it can't be moved."}
        free = self.data["open_slots"].get(apt["doctor"], [])
        if new_slot not in free:
            return {"status": "rejected", "message": f"{human_slot(new_slot)} isn't an open slot for {apt['doctor']}.",
                    "open_slots": free}
        free.remove(new_slot)
        free.append(apt["slot"])
        free.sort()
        old, apt["slot"] = apt["slot"], new_slot
        self._record(actor, "reschedule", appointment=appointment_id, old_slot=old, new_slot=new_slot)
        return {"status": "done", "appointment_id": appointment_id, "doctor": apt["doctor"], "old_slot": old,
                "new_slot": new_slot, "new_slot_human": human_slot(new_slot),
                "message": f"Moved {appointment_id} with {apt['doctor']} to {human_slot(new_slot)}."}

    def cancel(self, actor: Actor, appointment_id: str, reason: str = "") -> dict:
        apt, err = self._owned(actor, appointment_id)
        if err:
            return err
        if apt["status"] != "booked":
            return {"status": "rejected", "message": f"{appointment_id} is already {apt['status']}."}
        apt["status"] = "cancelled"
        self.data["open_slots"].setdefault(apt["doctor"], []).append(apt["slot"])
        self.data["open_slots"][apt["doctor"]].sort()
        self._record(actor, "cancel", appointment=appointment_id, reason=reason)
        return {"status": "done", "appointment_id": appointment_id, "doctor": apt["doctor"],
                "message": f"Cancelled {appointment_id} with {apt['doctor']}."}

    def refund_copay(self, actor: Actor, appointment_id: str, amount_inr: float) -> dict:
        apt, err = self._owned(actor, appointment_id)
        if err:
            return err
        try:
            amount = float(amount_inr)
        except (TypeError, ValueError):
            return {"status": "rejected", "message": "The refund amount isn't a number."}
        if apt["status"] != "cancelled":
            return {"status": "rejected", "message": "Co-pay refunds are only for cancelled appointments."}
        if apt["copay_refunded"]:
            return {"status": "rejected", "message": "This co-pay has already been refunded."}
        if amount <= 0 or amount > apt["copay_inr"]:
            return {"status": "rejected", "message": f"A refund must be between ₹1 and the ₹{apt['copay_inr']} paid."}
        apt["copay_refunded"] = True
        self._record(actor, "refund_copay", appointment=appointment_id, amount_inr=amount)
        return {"status": "done", "appointment_id": appointment_id, "refunded_inr": f"{amount:.0f}",
                "message": f"Refunded ₹{amount:.0f} co-pay for {appointment_id}."}

    # ------------------------------------------------------------------ views

    def appointments_view(self) -> list[dict[str, Any]]:
        return [{"id": apt_id, "patient": self.data["patients"][a["patient_id"]]["name"], **a}
                for apt_id, a in self.data["appointments"].items()]
