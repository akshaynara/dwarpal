"""Caller-side agents: what a customer's AI agent looks like when it talks to a Dwarpal-protected business.

A caller holds its own key, a passport from an issuer, and signs every request. Any A2A client can
do this; the helper below is ~40 lines on top of the standard a2a-sdk client.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
from a2a.client import ClientConfig, create_client
from a2a.helpers.proto_helpers import get_data_parts, get_text_parts, new_data_part, new_message, new_text_part
from a2a.types import Message, Role, SendMessageRequest

from ..a2a_server import EXTENSION_URI
from ..httpsig import Rfc9421Auth
from ..identity import KeyPair, sign_request


def parse_jsonrpc(data: dict[str, Any]) -> "CallResult":
    msg = ((data.get("result") or {}).get("message") or {})
    parts = msg.get("parts") or []
    texts = [p["text"] for p in parts if "text" in p]
    datas = [p["data"] for p in parts if isinstance(p.get("data"), dict)]
    return CallResult(reply="\n".join(texts), payload=datas[0] if datas else {})


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

    @property
    def challenge_id(self) -> str | None:
        return (self.payload.get("step_up") or {}).get("challenge_id")


class CallerAgent:
    def __init__(self, *, name: str, key: KeyPair, passport: str | None, audience: str,
                 http: httpx.AsyncClient, gateway_url: str, signing: str = "rfc9421"):
        """signing: "rfc9421" signs each HTTP request (RFC 9421, the default); "jws" puts a Dwarpal request
        proof in the message metadata instead."""
        self.name = name
        self.key = key
        self.passport = passport
        self.audience = audience
        self.http = http
        self.gateway_url = gateway_url
        self.signing = signing if passport else "none"
        self.auth = Rfc9421Auth(key, passport) if self.signing == "rfc9421" and passport else None
        self._client = None

    async def client(self):
        if self._client is None:
            # Same transport, plus the A2A extension header and, if it holds a passport, a request signer.
            http = httpx.AsyncClient(transport=self.http._transport, base_url=self.http.base_url,
                                     timeout=self.http.timeout, auth=self.auth,
                                     headers={**dict(self.http.headers), "A2A-Extensions": EXTENSION_URI})
            self._client = await create_client(self.gateway_url, ClientConfig(streaming=False, httpx_client=http))
        return self._client

    def prepare(self, *, intent: str | None = None, params: dict[str, Any] | None = None, note: str | None = None,
                text: str = "", session: str | None = None, tag: str | None = None, sign: bool = True,
                step_up: str | None = None, lane_code: str | None = None) -> Message:
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
        if self.passport:
            msg.extensions.append(EXTENSION_URI)
        meta: dict[str, Any] = {}
        if self.passport:
            meta["dwarpal_passport"] = self.passport
        signed: dict[str, Any] = {"envelope": envelope, "text": text}
        if step_up:
            meta["dwarpal_step_up"] = step_up
            signed["step_up"] = step_up  # the approval id is covered by the signature too
        if lane_code:
            meta["dwarpal_lane_code"] = lane_code
            signed["lane_code"] = lane_code
        if sign and self.passport and self.signing == "jws":
            meta["dwarpal_proof"] = sign_request(self.key, audience=self.audience, body=signed)
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

    async def replay_last(self, tamper: Callable[[bytes], bytes] | None = None) -> CallResult:
        """Resend the last signed HTTP request byte for byte (optionally with an edited body), as an
        attacker who captured it would. Used by the replay demo."""
        assert self.auth is not None and self.auth.last is not None, "nothing signed yet"
        method, url, headers, body = self.auth.last
        if tamper is not None:
            body = tamper(body)
        headers = {k: v for k, v in headers.items() if k.lower() != "content-length"}
        r = await self.http.request(method, url, content=body, headers=headers)
        return parse_jsonrpc(r.json())
