"""Command line: `dwarpal serve`, `dwarpal demo`, `dwarpal verify-audit`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time

STATUS_ICON = {"pass": "✔", "fail": "✘", "warn": "!", "handoff": "→", "skip": "·"}
OUTCOME_COLOR = {"completed": "32", "rejected": "33", "denied": "31", "escalated": "35", "needs_structure": "36",
                 "step_up_required": "35", "approved": "32", "needs_more": "33", "joined": "32", "error": "31"}


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    os.environ.setdefault("DWARPAL_PUBLIC_URL", f"http://localhost:{args.port}")
    from .app import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info",
                proxy_headers=True, forwarded_allow_ips="*")  # behind Cloud Run / a load balancer


async def _demo(only: list[str] | None, verbose: bool, businesses: list[str] | None) -> int:
    import httpx

    from .app import create_app, stage_for
    from .demo import scenarios as sc

    app = create_app(businesses=businesses)
    failures = 0
    for rt in app.state.runtimes.values():
        todo = [x for x in rt.kit.scenarios if not only or x.id in only]
        if not todo:
            continue
        print(_c("1", f"\n══ {rt.policy.business_name} · LLM: {rt.settings.resolved_llm_mode} ══\n"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=rt.settings.public_url,
                                     timeout=120) as http:
            for scenario in todo:
                rt.service.reset()
                result = await sc.run_scenario(scenario.id, stage_for(rt, http), rt.kit.scenarios)
                ok = result["as_expected"]
                failures += 0 if ok else 1
                print(_c("1", f"▶ {scenario.title}") + ("" if ok else _c("31", "  (unexpected outcome)")))
                print(f"  {scenario.story}")
                for step in result["steps"]:
                    outcome = step["outcome"]
                    print(f"   • {step['label']}")
                    print(f"     {_c(OUTCOME_COLOR.get(outcome, '0'), outcome.upper())}  {step['reply']}")
                    cm = step["payload"].get("customer_message")
                    if cm and (verbose or outcome not in {"completed"}):
                        print(f"     {'caller hears' if 'say' in step['payload'] else 'customer hears'}: “{cm}”")
                    for chk in step["payload"].get("checks", []):
                        if verbose or chk["status"] in {"fail", "handoff"}:
                            print(f"       {STATUS_ICON.get(chk['status'], '?')} {chk['name']}: {chk['detail']}")
                print()
        verdict = rt.gateway.audit.verify()
        print(_c("1", "Audit chain: ") + (_c("32", f"intact, {verdict['count']} signed records")
                                          if verdict["ok"] else _c("31", f"BROKEN at #{verdict['broken_at']}")))
        print(f"Human queue: {len(rt.gateway.escalations.tickets)} ticket(s)")
    print()
    return 1 if failures else 0


def cmd_demo(args: argparse.Namespace) -> None:
    sys.exit(asyncio.run(_demo(args.only, args.verbose, args.business)))


def cmd_pas(args: argparse.Namespace) -> None:
    """Run the bundled PAS-style FHIR server on its own, so the payor's FHIR rail calls it over the network."""
    import uvicorn
    from fastapi import FastAPI

    from .business.payor import PayorService
    from .fhir.pas_server import pas_router

    svc = PayorService()
    token = args.token or os.getenv("DWARPAL_PAYOR_FHIR_TOKEN") or "dev-token"
    app = FastAPI(title="Brightline PAS test server")
    app.include_router(pas_router(lambda: svc.data, token=token, is_down=lambda: False))
    print(f"PAS test server on http://{args.host}:{args.port}  (bearer token: {token})")
    print(f"Point Dwarpal at it: DWARPAL_PAYOR_FHIR_URL=http://{args.host}:{args.port} "
          f"DWARPAL_PAYOR_FHIR_TOKEN={token} dwarpal serve")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


async def _doctor(live: bool) -> int:
    """Check this installation end to end and say plainly what works."""
    import platform

    import httpx

    from . import __version__
    from .app import create_app, stage_for
    from .demo import scenarios as sc

    results: list[tuple[str, str, str]] = []

    def add(status: str, name: str, detail: str) -> None:
        results.append((status, name, detail))
        _line(status, name, detail)

    add("PASS" if sys.version_info >= (3, 11) else "FAIL", "Python", platform.python_version())
    add("PASS", "Dwarpal", __version__)
    try:
        app = create_app()
    except Exception as exc:  # noqa: BLE001
        add("FAIL", "Startup", f"{type(exc).__name__}: {exc}")
        return _report(results)
    for rt in app.state.runtimes.values():
        s = rt.settings
        add("PASS", f"{rt.kit.slug}: policy", f"{rt.policy.business_name}, {len(rt.policy.intents)} actions")
        add("PASS", f"{rt.kit.slug}: audit chain", "intact" if rt.gateway.audit.verify()["ok"] else "BROKEN")
        issuers = [i.label or i.issuer for i in rt.trust.issuers()]
        add("PASS" if issuers else "FAIL", f"{rt.kit.slug}: trusted issuers", ", ".join(issuers) or "none")
    s = next(iter(app.state.runtimes.values())).settings
    add("PASS", "Mode", f"{s.mode}; demo endpoints {'on' if s.enable_demo_endpoints else 'off'}; state {s.state}")
    if s.production:
        add("PASS" if s.idp_key else "WARN", "IdP callback key",
            "set" if s.idp_key else "DWARPAL_IDP_KEY unset: nobody can approve step-ups in production")

    mode = s.resolved_llm_mode
    add("PASS", "LLM mode", f"{mode}" + (f" ({s.model})" if mode == "gemini" else
                                         ": offline scripted model. Set GOOGLE_API_KEY to use Gemini."))
    live_gemini_failed = False
    if mode == "gemini":
        try:
            from google import genai

            from google.genai import errors, types

            print(f"  ...   asking Gemini ({s.model}), up to {s.llm_timeout:g}s", flush=True)
            started = time.monotonic()
            # One attempt, so Google's own error shows instead of silent retries running out the clock.
            client = genai.Client(http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1)))
            from .llm import fast_config

            reply = await asyncio.wait_for(client.aio.models.generate_content(
                model=s.model, contents="Reply with the word OK.", config=fast_config(s.model)), s.llm_timeout)
            add("PASS", "Gemini call",
                f"{s.model} replied in {time.monotonic() - started:.1f}s: {(reply.text or '').strip()[:40]}")
        except TimeoutError:
            add("FAIL", "Gemini call", f"no reply in {s.llm_timeout:g}s. Something on the network (a VPN, office "
                "proxy or firewall) may be holding the request; try another network. "
                "The demo still works: the scripted fallback answers.")
            live_gemini_failed = True
        except errors.APIError as exc:
            hint = {503: f"{s.model} is overloaded right now; checking other models below.",
                    429: "quota or rate limit reached for this key. Wait a minute, or check limits in AI Studio.",
                    404: f"{s.model} isn't available to this key (Google retires models for new keys); "
                         "checking other models below.",
                    400: "Google rejected the request; often an invalid key.",
                    403: "this key isn't allowed to call Gemini (API disabled or key restricted)."}.get(exc.code, "")
            add("FAIL", "Gemini call", f"HTTP {exc.code} {exc.status or ''}: Google says \"{str(exc.message)[:160]}\" "
                f"{hint} The demo still works: the scripted fallback answers.")
            live_gemini_failed = True
            if exc.code in (404, 503):
                working = await _find_working_model(client, s.model)
                if working:
                    add("WARN", "Gemini model", f"{working} answers for this key. Use it: "
                        f"export DWARPAL_MODEL={working}")
                else:
                    add("WARN", "Gemini model", "no other Flash model answered either; try again in a few minutes.")
        except Exception as exc:  # noqa: BLE001
            add("FAIL", "Gemini call", f"{type(exc).__name__}: {str(exc)[:160]}")
            live_gemini_failed = True
    if live and live_gemini_failed:
        add("WARN", "End-to-end scenario", "skipped: Gemini didn't answer. Run with DWARPAL_LLM_MODE=mock "
            "to check the rest, or retry once Gemini answers.")
        live = False
    if live:
        rt = app.state.runtimes.get("clinic") or next(iter(app.state.runtimes.values()))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=s.public_url, timeout=120) as http:
            sid = rt.kit.scenarios[0].id if rt.kit.slug != "clinic" else "reschedule"
            res = await sc.run_scenario(sid, stage_for(rt, http), rt.kit.scenarios)
        detail = " ".join(c["detail"] for st in res["steps"] for c in st["payload"].get("checks", [])
                          if c["name"] == "data_guard")
        fell_back = "scripted fallback" in detail
        add("PASS" if res["as_expected"] and not fell_back else "FAIL" if not res["as_expected"] else "WARN",
            "End-to-end scenario", f"{sid}: {res['outcomes']}" + (" (Gemini failed, scripted fallback answered)"
                                                                   if fell_back else ""))
    if os.getenv("DWARPAL_PAYOR_FHIR_URL"):
        url = os.getenv("DWARPAL_PAYOR_FHIR_URL", "").rstrip("/") + "/metadata"
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                r = await http.get(url)
            add("PASS" if r.status_code == 200 else "FAIL", "Payor FHIR endpoint", f"{url} → HTTP {r.status_code}")
        except Exception as exc:  # noqa: BLE001
            add("FAIL", "Payor FHIR endpoint", f"{url}: {type(exc).__name__}")
    return _report(results)


async def _find_working_model(client, failed: str) -> str | None:
    """Ask Google which Flash models this key can use, and return the first that actually answers."""
    skip = ("image", "tts", "audio", "live", "embedding", "thinking", "exp", "preview", "8b", "omni")
    try:
        names = [m.name.removeprefix("models/") async for m in await client.aio.models.list()
                 if "generateContent" in (m.supported_actions or []) and "flash" in (m.name or "")]
    except Exception:  # noqa: BLE001
        return None
    candidates = [n for n in names if n != failed and not any(k in n for k in skip)]
    candidates.sort(reverse=True)                                     # newest version numbers first,
    candidates.sort(key=lambda n: ("latest" in n, "lite" in n))       # full Flash before Lite and aliases
    candidates = candidates[:5]
    print(f"  ...   trying other models: {', '.join(candidates) or 'none listed'}", flush=True)
    for name in candidates:
        try:
            await asyncio.wait_for(client.aio.models.generate_content(model=name, contents="Reply with OK."), 10)
            print(f"        {name}: answered", flush=True)
            return name
        except TimeoutError:
            print(f"        {name}: no reply in 10s", flush=True)
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", "")
            msg = str(getattr(exc, "message", "") or exc)[:110]
            print(f"        {name}: {code} {msg}".rstrip(), flush=True)
    return None


def _line(status: str, name: str, detail: str) -> None:
    colour = {"PASS": "32", "WARN": "33", "FAIL": "31"}
    print(f"  {_c(colour[status], status):>4}  {name}: {detail}", flush=True)


def _report(results: list[tuple[str, str, str]]) -> int:
    bad = sum(1 for r in results if r[0] == "FAIL")
    print(f"\n{'All good.' if not bad else f'{bad} problem(s) found.'}")
    return 1 if bad else 0


def cmd_doctor(args: argparse.Namespace) -> None:
    print(_c("1", "\nDwarpal doctor\n"))
    sys.exit(asyncio.run(_doctor(not args.quick)))


def cmd_keygen(args: argparse.Namespace) -> None:
    """Create an Ed25519 key (for an issuer, or for an agent) and print its kid and public key."""
    from pathlib import Path

    from .identity import KeyPair

    path = Path(args.out)
    if path.exists():
        sys.exit(f"{path} already exists; not overwriting it.")
    kp = KeyPair.load_or_create(path)
    print(f"private key: {path} (keep it secret)\nkid:         {kp.kid}\npublic_key:  {kp.public_b64}")
    print("\nTo trust it as an issuer, add to the policy's trusted_issuers:\n"
          f"  - id: <issuer id>\n    public_key: {kp.public_b64}")


def cmd_issue(args: argparse.Namespace) -> None:
    """Issue an agent passport (for a pilot, or when the business itself is the identity provider)."""
    from pathlib import Path

    from .identity import KeyPair, Principal, issue_passport

    issuer = KeyPair.load_or_create(Path(args.issuer_key))

    class _Agent:  # issue_passport needs only the agent's kid and public key
        def __init__(self, pub_b64: str):
            from .identity import b64u_decode
            import hashlib

            self.public_b64, self.kid = pub_b64, hashlib.sha256(b64u_decode(pub_b64)).hexdigest()[:16]

    agent_pub = KeyPair.load_or_create(Path(args.agent_key)).public_b64 if args.agent_key else args.agent_pub
    if not agent_pub:
        sys.exit("Give --agent-key (a key file) or --agent-pub (its public key).")
    token = issue_passport(
        issuer, issuer=args.issuer, agent_key=_Agent(agent_pub), agent_id=args.agent_id,
        agent_name=args.agent_name or args.agent_id,
        principal=Principal(id=args.principal_id, name=args.principal_name or args.principal_id, type=args.principal_type),
        audience=args.audience, scopes=[x.strip() for x in args.scopes.split(",") if x.strip()],
        limits={"max_amount_inr": args.max_amount} if args.max_amount else {}, ttl_seconds=args.ttl)
    print(token)


def cmd_verify(args: argparse.Namespace) -> None:
    from .app import build_runtime

    v = build_runtime().gateway.audit.verify()
    print(v)
    sys.exit(0 if v["ok"] else 1)


def main() -> None:
    # Settings from a .env file in the current folder; variables already exported take precedence.
    from dotenv import load_dotenv

    load_dotenv(".env", override=False)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="dwarpal", description="Trust and security gateway for inbound AI agents.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="Run the gateway (A2A endpoint + dashboard).")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=int(os.getenv("PORT", "8080")))
    s.set_defaults(func=cmd_serve)
    d = sub.add_parser("demo", help="Run the demo scenarios in the terminal.")
    d.add_argument("--only", nargs="*", help="Scenario ids to run.")
    d.add_argument("-v", "--verbose", action="store_true", help="Show every check, not just failures.")
    d.add_argument("--business", nargs="*", help="Businesses to run (clinic, payor). Default: all.")
    d.set_defaults(func=cmd_demo)
    dr = sub.add_parser("doctor", help="Check the installation, Gemini and an end-to-end scenario.")
    dr.add_argument("--quick", action="store_true", help="Skip the end-to-end scenario.")
    dr.set_defaults(func=cmd_doctor)
    k = sub.add_parser("keygen", help="Create an Ed25519 key for an issuer or an agent.")
    k.add_argument("--out", required=True, help="Where to write the private key (PEM).")
    k.set_defaults(func=cmd_keygen)
    i = sub.add_parser("issue-passport", help="Issue an agent passport signed by an issuer key.")
    i.add_argument("--issuer-key", required=True)
    i.add_argument("--issuer", required=True, help="Issuer id, as listed in the policy's trusted_issuers.")
    i.add_argument("--audience", required=True, help="The business id (policy business.id).")
    i.add_argument("--agent-id", required=True)
    i.add_argument("--agent-name")
    g = i.add_mutually_exclusive_group()
    g.add_argument("--agent-key", help="The agent's key file (PEM).")
    g.add_argument("--agent-pub", help="The agent's public key (base64url).")
    i.add_argument("--principal-id", required=True)
    i.add_argument("--principal-name")
    i.add_argument("--principal-type", default="person")
    i.add_argument("--scopes", required=True, help="Comma-separated, e.g. appointments:read,appointments:write")
    i.add_argument("--max-amount", type=float, default=None)
    i.add_argument("--ttl", type=int, default=3600)
    i.set_defaults(func=cmd_issue)
    f = sub.add_parser("pas-server", help="Run the bundled PAS-style FHIR server on its own.")
    f.add_argument("--host", default="127.0.0.1")
    f.add_argument("--port", type=int, default=8090)
    f.add_argument("--token", default=None)
    f.set_defaults(func=cmd_pas)
    v = sub.add_parser("verify-audit", help="Verify the audit hash chain and signatures.")
    v.set_defaults(func=cmd_verify)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
