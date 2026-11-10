"""How the gateway reaches the business agent it protects.

* ADKBackend         — the agent runs in-process with an ADK Runner (the demo).
* A2AUpstreamBackend — the agent is its own A2A service (for example on Vertex AI Agent Engine or
                       Cloud Run) and Dwarpal is the only thing allowed to call it.

Either way the business agent receives a *verified task* built by the gateway: the validated
intent, whitelisted parameters and the verified principal. The caller's raw text never reaches it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from google.adk.agents import BaseAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

log = logging.getLogger("dwarpal.backend")


@dataclass
class BusinessTask:
    session_id: str
    agent_id: str
    agent_name: str
    principal_id: str
    principal_name: str
    intent: str
    params: dict[str, Any]

    def render(self) -> str:
        return (
            "DWARPAL VERIFIED TASK\n"
            f"caller_agent: {self.agent_name} ({self.agent_id})\n"
            f"acting_for: {self.principal_name} ({self.principal_id})\n"
            f"intent: {self.intent}\n"
            f"params: {json.dumps(self.params, ensure_ascii=False)}\n"
        )


@dataclass
class BusinessResult:
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    model_calls: int = 0
    tool_calls: list[str] = field(default_factory=list)
    fallback: bool = False


class BusinessBackend(Protocol):
    async def run(self, task: BusinessTask) -> BusinessResult: ...


class ADKBackend:
    """Runs the business agent in-process. An optional fallback agent (the scripted model) takes over if
    the primary model errors, so a live demo survives a quota or network blip."""

    APP = "dwarpal_business"

    def __init__(self, agent: BaseAgent, fallback: BaseAgent | None = None):
        self.agent = agent
        self.sessions = InMemorySessionService()
        self.runner = Runner(app_name=self.APP, agent=agent, session_service=self.sessions)
        self.fallback = fallback
        self.fallback_runner = (Runner(app_name=self.APP, agent=fallback, session_service=self.sessions)
                                if fallback else None)
        self._known: set[str] = set()

    async def run(self, task: BusinessTask) -> BusinessResult:
        try:
            return await self._run(task, self.runner, self.agent)
        except Exception as exc:  # noqa: BLE001
            if not self.fallback_runner:
                raise
            log.warning("Primary model failed (%s); answering with the scripted fallback.", exc)
            result = await self._run(task, self.fallback_runner, self.fallback, fresh_session=True)
            result.fallback = True
            return result

    async def _run(self, task: BusinessTask, runner: Runner, agent: BaseAgent,
                   fresh_session: bool = False) -> BusinessResult:
        sid = f"{task.agent_id}:{task.session_id}" + (":fallback" if fresh_session else "")
        user = task.agent_id
        if sid not in self._known:
            await self.sessions.create_session(app_name=self.APP, user_id=user, session_id=sid,
                                               state={"principal_id": task.principal_id})
            self._known.add(sid)
        result = BusinessResult(message="")
        texts: list[str] = []
        msg = types.Content(role="user", parts=[types.Part(text=task.render())])
        async for event in runner.run_async(user_id=user, session_id=sid, new_message=msg):
            if not event.content or not event.content.parts:
                continue
            parts = event.content.parts
            if any(p.function_call or p.text for p in parts) and event.author == agent.name:
                result.model_calls += 1
            for p in parts:
                if p.function_call:
                    result.tool_calls.append(p.function_call.name)
                if p.function_response and isinstance(p.function_response.response, dict):
                    result.data = dict(p.function_response.response)
                if p.text and event.author == agent.name and not p.thought:
                    texts.append(p.text)
        result.message = (texts[-1] if texts else result.data.get("message", "")).strip()
        return result


class A2AUpstreamBackend:
    """Forward verified tasks to a business agent exposed over A2A."""

    def __init__(self, url: str, httpx_client: Any | None = None):
        self.url = url
        self.httpx_client = httpx_client

    async def run(self, task: BusinessTask) -> BusinessResult:
        import httpx
        from a2a.client import ClientConfig, create_client
        from a2a.helpers.proto_helpers import get_data_parts, get_message_text, new_data_part, new_message, new_text_part
        from a2a.types import Role, SendMessageRequest

        client_http = self.httpx_client or httpx.AsyncClient(timeout=60)
        client = await create_client(self.url, ClientConfig(streaming=False, httpx_client=client_http))
        message = new_message([new_text_part(task.render()), new_data_part(task.__dict__)],
                              context_id=f"{task.agent_id}:{task.session_id}", role=Role.ROLE_USER)
        result = BusinessResult(message="", model_calls=1)
        async for resp in client.send_message(SendMessageRequest(message=message)):
            if resp.HasField("message"):
                result.message = get_message_text(resp.message)
                data = get_data_parts(resp.message.parts)
                if data and isinstance(data[0], dict):
                    result.data = data[0]
        return result
