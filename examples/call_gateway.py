"""A customer's AI agent calling a running Dwarpal gateway over A2A.

    dwarpal serve                       # in one terminal
    python examples/call_gateway.py     # in another

Uses the demo identity keys the server generated in .dwarpal/, so run it from the repo root.
"""

import asyncio
import json
import os
from pathlib import Path

import httpx

from dwarpal.demo.world import RIYA, DemoWorld, ISSUER_ID
from dwarpal.demo.callers import CallerAgent
from dwarpal.identity import issue_passport

GATEWAY = os.getenv("DWARPAL_URL", "http://localhost:8080")
AUDIENCE = "clinic.sunrise.example"


async def main() -> None:
    world = DemoWorld.load(Path(os.getenv("DWARPAL_DATA_DIR", ".dwarpal")), AUDIENCE)
    agent_key = world.keys["riya_assistant"]

    # 1. The patient's identity wallet issues a passport to her assistant.
    passport = issue_passport(
        world.issuer_key, issuer=ISSUER_ID, agent_key=agent_key, agent_id="agent:riya-assistant",
        agent_name="Riya's Assistant", principal=RIYA, audience=AUDIENCE,
        scopes=["appointments:read", "appointments:write"], ttl_seconds=900,
    )

    async with httpx.AsyncClient(timeout=60) as http:
        print("Agent card:", (await http.get(f"{GATEWAY}/.well-known/agent-card.json")).json()["name"])
        caller = CallerAgent(name="Riya's Assistant", key=agent_key, passport=passport, audience=AUDIENCE,
                             http=http, gateway_url=GATEWAY)

        # 2. Every request is a structured intent, signed by the agent's own key.
        result = await caller.send(intent="list_slots", params={"doctor": "Dr. Mehta", "date": "2026-11-13"})
        print(f"\n{result.outcome.upper()}: {result.reply}")
        for check in result.payload["checks"]:
            print(f"  {check['status']:>7}  {check['name']}: {check['detail']}")

        result = await caller.send(intent="medical_records", params={"record_type": "prescription"})
        print(f"\n{result.outcome.upper()}: {result.reply}")
        print(json.dumps(result.payload["data"], indent=2))


if __name__ == "__main__":
    asyncio.run(main())
