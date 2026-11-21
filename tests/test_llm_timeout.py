"""A slow or unreachable Gemini must never freeze the gateway: the scripted fallback answers in time."""

import asyncio
import time

from dwarpal.business.backend import ADKBackend, BusinessTask
from dwarpal.business.clinic import build_clinic_agent
from dwarpal.business.service import ClinicService


async def test_hung_model_falls_back_within_the_timeout():
    service = ClinicService()
    primary = build_clinic_agent(service, use_gemini=False, model="x")
    fallback = build_clinic_agent(service, use_gemini=False, model="x")
    backend = ADKBackend(primary, fallback, timeout=0.5)
    real_run = backend._run

    async def run(task, runner, agent, fresh_session=False):
        if runner is backend.runner:      # the "Gemini" path hangs, like a blocked network
            await asyncio.sleep(60)
        return await real_run(task, runner, agent, fresh_session)

    backend._run = run
    task = BusinessTask(session_id="s", agent_id="agent:t", agent_name="T", principal_id="", principal_name="",
                        intent="clinic_hours", params={}, tier="open")
    started = time.monotonic()
    result = await backend.run(task)
    assert result.fallback is True
    assert time.monotonic() - started < 5
