"""Caller-side agents: what a customer's AI agent looks like when it talks to a Dwarpal-protected business.

A caller holds its own key, a passport from an issuer, and signs every request. Any A2A client can
do this; the helper below is ~40 lines on top of the standard a2a-sdk client.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from a2a.client import ClientConfig, create_client
from a2a.helpers.proto_helpers import get_data_parts, get_text_parts, new_data_part, new_message, new_text_part
from a2a.types import Message, Role, SendMessageRequest

from ..identity import KeyPair, sign_request


@dataclass
class CallResult:
    reply: str
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def outcome(self) -> str:
        return self.payload.get("outcome", "?")

    @property
    def data(self) -> dict[str, Any]:
        return self.payload.get("data") or {}


class CallerAgent:
    def __init__(self, *, name: str, key: KeyPair, passport: str | None, audience: str,
                 http: httpx.AsyncClient, gateway_url: str):
        self.name = name
        self.key = key
        self.passport = passport
        self.audience = audience
        self.http = http
        self.gateway_url = gateway_url
        self._client = None

    async def client(self):
        if self._client is None:
            self._client = await create_client(self.gateway_url, ClientConfig(streaming=False, httpx_client=self.http))
        return self._client

    def prepare(self, *, intent: str | None = None, params: dict[str, Any] | None = None, note: str | None = None,
                text: str = "", session: str | None = None, tag: str | None = None, sign: bool = True) -> Message:
        envelope = None
        if intent:
            envelope = {"intent": intent, "params": params or {}}
            if note:
                envelope["note"] = note
        parts = []
        if text:
            parts.append(new_text_part(text))
        if envelope is not None:
            parts.append(new_data_part(envelope))
        msg = new_message(parts, context_id=session or f"sess-{uuid.uuid4().hex[:8]}", role=Role.ROLE_USER)
        meta: dict[str, Any] = {}
        if self.passport:
            meta["dwarpal_passport"] = self.passport
        if sign:
            meta["dwarpal_proof"] = sign_request(self.key, audience=self.audience,
                                                 body={"envelope": envelope, "text": text})
        if tag:
            meta["dwarpal_tag"] = tag
        msg.metadata.update(meta)
        return msg

    async def deliver(self, message: Message) -> CallResult:
        msg = copy.deepcopy(message)
        msg.message_id = str(uuid.uuid4())  # transport-level id; the signed body is unchanged
        client = await self.client()
        result = CallResult(reply="")
        async for resp in client.send_message(SendMessageRequest(message=msg)):
            if resp.HasField("message"):
                result.reply = "\n".join(get_text_parts(resp.message.parts))
                data = [d for d in get_data_parts(resp.message.parts) if isinstance(d, dict)]
                result.payload = data[0] if data else {}
        return result

    async def send(self, **kwargs: Any) -> CallResult:
        return await self.deliver(self.prepare(**kwargs))
