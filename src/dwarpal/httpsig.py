"""HTTP Message Signatures (RFC 9421) with Content-Digest (RFC 9530), Ed25519.

Callers sign the HTTP request itself, so the whole JSON-RPC body (passport, envelope, step-up id,
lane code) is covered without a Dwarpal-specific proof format. It is the same approach as Web Bot
Auth and Visa/Cloudflare's Trusted Agent Protocol, profiled for agents that carry a passport:

  Authorization:   AgentPassport <passport JWS>
  Content-Digest:  sha-256=:<base64 of SHA-256(body)>:
  Signature-Input: sig1=("@method" "@authority" "@path" "content-digest" "content-type" "authorization")
                   ;created=<unix>;keyid="<passport cnf kid>";alg="ed25519";nonce="<random>";tag="dwarpal-agent"
  Signature:       sig1=:<base64 Ed25519 signature over the signature base>:

The signing key must be the one the passport binds (its `cnf`). The ASGI middleware verifies the
signature and digest at the HTTP layer and hands the result to the gateway in an internal header
(any client-supplied copy is stripped). The gateway then verifies the passport, checks the key
and passport match, and enforces nonce replay protection.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from typing import Any, Callable

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .identity import KeyPair, b64u, b64u_decode, public_key_from_b64

COMPONENTS = ("@method", "@authority", "@path", "content-digest", "content-type", "authorization")
REQUIRED = {"@method", "@authority", "@path", "content-digest", "authorization"}
TAG = "dwarpal-agent"
LABEL = "sig1"
INTERNAL_HEADER = "x-dwarpal-httpsig"


def content_digest(body: bytes) -> str:
    return "sha-256=:" + base64.b64encode(hashlib.sha256(body).digest()).decode() + ":"


def _params(components: tuple[str, ...] | list[str], *, created: int, keyid: str, nonce: str) -> str:
    inner = "(" + " ".join(f'"{c}"' for c in components) + ")"
    return f'{inner};created={created};keyid="{keyid}";alg="ed25519";nonce="{nonce}";tag="{TAG}"'


def signature_base(values: dict[str, str], components: list[str] | tuple[str, ...], params: str) -> bytes:
    lines = [f'"{c}": {values[c]}' for c in components]
    lines.append(f'"@signature-params": {params}')
    return "\n".join(lines).encode()


def _values(method: str, authority: str, path: str, headers: dict[str, str]) -> dict[str, str]:
    h = {k.lower(): v.strip() for k, v in headers.items()}
    return {"@method": method.upper(), "@authority": authority.lower(), "@path": path or "/", **h}


def sign(key: KeyPair, *, method: str, authority: str, path: str, headers: dict[str, str], body: bytes,
         now: int | None = None, nonce: str | None = None) -> dict[str, str]:
    """Return the headers to add: Content-Digest, Signature-Input, Signature."""
    out = {"content-digest": content_digest(body)}
    values = _values(method, authority, path, {**headers, **out})
    comps = [c for c in COMPONENTS if c in values]
    params = _params(comps, created=int(now if now is not None else time.time()), keyid=key.kid,
                     nonce=nonce or secrets.token_urlsafe(16))
    sig = key.sign(signature_base(values, comps, params))
    out["signature-input"] = f"{LABEL}={params}"
    out["signature"] = f"{LABEL}=:{base64.b64encode(sig).decode()}:"
    return out


_SIG_INPUT = re.compile(r'^\s*([A-Za-z0-9_-]+)=(\((?:"[^"]+"\s*)*\)(?:;[a-z]+=(?:"[^"]*"|-?\d+))*)\s*$')
_SIG = re.compile(r'^\s*([A-Za-z0-9_-]+)=:([A-Za-z0-9+/=]+):\s*$')


def parse_signature_input(value: str) -> tuple[str, list[str], dict[str, Any], str]:
    m = _SIG_INPUT.match(value)
    if not m:
        raise ValueError("malformed Signature-Input")
    label, params_str = m.group(1), m.group(2)
    comps = re.findall(r'"([^"]+)"', params_str[: params_str.index(")") + 1])
    params: dict[str, Any] = {}
    for k, v in re.findall(r';([a-z]+)=("[^"]*"|-?\d+)', params_str[params_str.index(")") + 1:]):
        params[k] = v.strip('"') if v.startswith('"') else int(v)
    return label, comps, params, params_str


def parse_signature(value: str) -> tuple[str, bytes]:
    m = _SIG.match(value)
    if not m:
        raise ValueError("malformed Signature")
    return m.group(1), base64.b64decode(m.group(2))


def verify(*, method: str, authority: str, path: str, headers: dict[str, str], body: bytes,
           key_for: Callable[[str], Ed25519PublicKey | None], now: float | None = None,
           max_age: int = 60, skew: int = 30) -> dict[str, Any]:
    """Verify an RFC 9421 signature. Returns {"valid": True, kid, created, nonce, components} or
    {"valid": False, "error": code, "message": ...}. Never raises."""
    now = time.time() if now is None else now
    h = {k.lower(): v for k, v in headers.items()}

    def fail(code: str, msg: str) -> dict[str, Any]:
        return {"valid": False, "error": code, "message": msg}

    if "signature-input" not in h or "signature" not in h:
        return fail("missing_signature", "No HTTP message signature on the request.")
    try:
        label, comps, params, params_str = parse_signature_input(h["signature-input"])
        sig_label, sig = parse_signature(h["signature"])
    except ValueError as exc:
        return fail("malformed_signature", str(exc))
    if label != sig_label:
        return fail("malformed_signature", "Signature and Signature-Input labels differ.")
    if not REQUIRED.issubset(comps):
        return fail("weak_signature", f"Signature must cover {sorted(REQUIRED)}; it covers {comps}.")
    if params.get("alg") not in (None, "ed25519") or params.get("tag") != TAG:
        return fail("wrong_profile", "Expected alg=ed25519 and tag=dwarpal-agent.")
    created = params.get("created")
    if not isinstance(created, int) or now - created > max_age or created - now > skew:
        return fail("stale_proof", f"Signature created {int(now - (created or 0))}s ago.")
    if not params.get("nonce"):
        return fail("missing_nonce", "Signature has no nonce.")
    if h.get("content-digest") != content_digest(body):
        return fail("body_tampered", "Request body does not match its Content-Digest.")
    values = _values(method, authority, path, h)
    if any(c not in values for c in comps):
        return fail("malformed_signature", "A covered component is missing from the request.")
    key = key_for(str(params.get("keyid", "")))
    if key is None:
        return fail("key_mismatch", "Signature keyid isn't the key the passport binds.")
    try:
        key.verify(sig, signature_base(values, comps, params_str))
    except Exception:  # noqa: BLE001
        return fail("bad_signature", "HTTP message signature does not verify.")
    return {"valid": True, "kid": params["keyid"], "created": created, "nonce": params["nonce"],
            "components": comps}


def passport_from_authorization(value: str | None) -> str | None:
    if value and value.lower().startswith("agentpassport "):
        return value.split(" ", 1)[1].strip()
    return None


def passport_key(passport: str) -> tuple[str, Ed25519PublicKey] | None:
    """The key a passport binds (cnf). Unverified decode: the gateway verifies the passport itself."""
    try:
        claims = json.loads(b64u_decode(passport.split(".")[1]))
        cnf = claims.get("cnf") or {}
        return str(cnf["kid"]), public_key_from_b64(cnf["pub"])
    except Exception:  # noqa: BLE001
        return None


def passport_sha(passport: str) -> str:
    return hashlib.sha256(passport.encode()).hexdigest()


# --------------------------------------------------------------------------- client side


class Rfc9421Auth(httpx.Auth):
    """Signs every request an agent sends. Keeps the last signed request so the demo can replay it."""
    requires_request_body = True

    def __init__(self, key: KeyPair, passport: str):
        self.key, self.passport = key, passport
        self.last: tuple[str, str, dict[str, str], bytes] | None = None

    def auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"AgentPassport {self.passport}"
        body = request.content
        path = request.url.raw_path.decode().split("?")[0]
        added = sign(self.key, method=request.method, authority=request.url.netloc.decode(), path=path,
                     headers={k: v for k, v in request.headers.items()}, body=body)
        for k, v in added.items():
            request.headers[k] = v
        self.last = (request.method, str(request.url), dict(request.headers), body)
        yield request


# --------------------------------------------------------------------------- server side


class HttpSignatureMiddleware:
    """Verifies RFC 9421 signatures on A2A endpoints and passes the result to the gateway in an internal
    header. Strips any client-supplied copy of that header from every request."""

    def __init__(self, app: Any, path_suffix: str = "/a2a"):
        self.app, self.suffix = app, path_suffix
        self.raw = INTERNAL_HEADER.encode()

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = [(k, v) for k, v in scope["headers"] if k.lower() != self.raw]
        scope = {**scope, "headers": headers}
        hmap = {k.decode().lower(): v.decode() for k, v in headers}
        if scope["method"] != "POST" or not scope["path"].endswith(self.suffix) or "signature-input" not in hmap:
            await self.app(scope, receive, send)
            return

        chunks, more = [], True
        while more:
            msg = await receive()
            if msg["type"] != "http.request":
                break
            chunks.append(msg.get("body", b""))
            more = msg.get("more_body", False)
        body = b"".join(chunks)

        passport = passport_from_authorization(hmap.get("authorization"))
        bound = passport_key(passport) if passport else None

        def key_for(kid: str) -> Ed25519PublicKey | None:
            return bound[1] if bound and bound[0] == kid else None

        if not passport:
            result = {"valid": False, "error": "missing_passport_header",
                      "message": "RFC 9421 requests carry the passport in Authorization: AgentPassport <jws>."}
        else:
            result = verify(method=scope["method"], authority=hmap.get("host", ""),
                            path=scope.get("root_path", "") + scope["path"], headers=hmap, body=body, key_for=key_for)
            result["passport_sha"] = passport_sha(passport)
        headers.append((self.raw, b64u(json.dumps(result).encode()).encode()))

        sent = False

        async def replay_receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app({**scope, "headers": headers}, replay_receive, send)


def read_internal(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        return json.loads(b64u_decode(value))
    except Exception:  # noqa: BLE001
        return {"valid": False, "error": "malformed_signature", "message": "Unreadable signature result."}
