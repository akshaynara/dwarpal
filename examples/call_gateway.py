"""A customer's AI agent calling a running Dwarpal gateway over A2A.

    dwarpal serve                       # in one terminal, then open http://localhost:8080
    python examples/call_gateway.py     # in another

It shows the three tiers a caller meets:
  1. open     - no passport needed
  2. account  - passport + signed request
  3. binding  - the gateway asks the patient to approve; approve it on the dashboard's "Riya's phone"
                card, and this script resends with the approval id

Uses the demo identity keys the server generated in .dwarpal/, so run it from the repo root.
"""

import asyncio
import os
from pathlib import Path

import httpx

from dwarpal.demo.callers import CallerAgent, CallResult
from dwarpal.demo.world import ISSUER_ID, RIYA, DemoWorld
from dwarpal.identity import issue_passport

GATEWAY = os.getenv("DWARPAL_URL", "http://localhost:8080")
AUDIENCE = "clinic.sunrise.example"


def show(title: str, r: CallResult) -> None:
    print(f"\n{title}\n  {r.outcome.upper()}: {r.reply}")
    if r.payload.get("customer_message"):
        print(f"  Customer hears: “{r.payload['customer_message']}”")
    for check in r.payload.get("checks", []):
        if check["status"] != "skip":
            print(f"  {check['status']:>7}  {check['name']}: {check['detail']}")


async def main() -> None:
    world = DemoWorld.load(Path(os.getenv("DWARPAL_DATA_DIR", ".dwarpal")), AUDIENCE)
    agent_key = world.keys["riya_assistant"]

    # The patient's identity wallet issues a passport to her assistant.
    passport = issue_passport(
        world.issuer_key, issuer=ISSUER_ID, agent_key=agent_key, agent_id="agent:riya-assistant",
        agent_name="Riya's Assistant", principal=RIYA, audience=AUDIENCE,
        scopes=["appointments:read", "appointments:write"], ttl_seconds=900,
    )

    async with httpx.AsyncClient(timeout=60) as http:
        print("Agent card:", (await http.get(f"{GATEWAY}/.well-known/agent-card.json")).json()["name"])
        guest = CallerAgent(name="Guest", key=world.keys["guest"], passport=None, audience=AUDIENCE,
                            http=http, gateway_url=GATEWAY)
        riya = CallerAgent(name="Riya's Assistant", key=agent_key, passport=passport, audience=AUDIENCE,
                           http=http, gateway_url=GATEWAY)

        show("1. Open tier, no passport: clinic hours", await guest.send(intent="clinic_info", params={"topic": "hours"}))
        show("2. Account tier: appointment status",
             await riya.send(intent="appointment_status", params={"appointment_id": "APT-501"}))

        params = {"appointment_id": "APT-501", "reason": "travelling"}
        first = await riya.send(intent="cancel_appointment", params=params, session="example-cancel")
        show("3. Binding tier: cancel", first)
        cid = first.challenge_id
        if not cid:
            return
        print(f"\n  Approve {cid} on the dashboard's “Riya's phone” card (waiting up to 2 minutes)…")
        for _ in range(30):
            await asyncio.sleep(4)
            r = await riya.send(intent="cancel_appointment", params=params, session="example-cancel", step_up=cid)
            if r.outcome != "step_up_required":
                show("   Resent with the approval id", r)
                return
        print("  No approval arrived; nothing was cancelled.")


if __name__ == "__main__":
    asyncio.run(main())
