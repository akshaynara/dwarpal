"""Command line: `dwarpal serve`, `dwarpal demo`, `dwarpal verify-audit`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

STATUS_ICON = {"pass": "✔", "fail": "✘", "warn": "!", "handoff": "→", "skip": "·"}
OUTCOME_COLOR = {"completed": "32", "rejected": "33", "denied": "31", "escalated": "35", "needs_structure": "36",
                 "error": "31"}


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    os.environ.setdefault("DWARPAL_PUBLIC_URL", f"http://localhost:{args.port}")
    from .app import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")


async def _demo(only: list[str] | None, verbose: bool) -> int:
    import httpx

    from .app import create_app
    from .demo import scenarios as sc

    app = create_app()
    rt = app.state.runtime
    print(_c("1", f"\nDwarpal demo · {rt.policy.business_name} · LLM: {rt.settings.resolved_llm_mode}\n"))
    failures = 0
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=rt.settings.public_url,
                                 timeout=120) as http:
        for scenario in sc.SCENARIOS:
            if only and scenario.id not in only:
                continue
            rt.clinic.reset()
            result = await sc.run_scenario(scenario.id, rt.world.callers(http, rt.settings.public_url))
            ok = result["as_expected"]
            failures += 0 if ok else 1
            print(_c("1", f"▶ {scenario.title}") + ("" if ok else _c("31", "  (unexpected outcome)")))
            print(f"  {scenario.story}")
            for step in result["steps"]:
                outcome = step["outcome"]
                print(f"   • {step['label']}")
                print(f"     {_c(OUTCOME_COLOR.get(outcome, '0'), outcome.upper())}  {step['reply']}")
                for chk in step["payload"].get("checks", []):
                    if verbose or chk["status"] in {"fail", "handoff"}:
                        print(f"       {STATUS_ICON.get(chk['status'], '?')} {chk['name']}: {chk['detail']}")
            print()
    verdict = rt.gateway.audit.verify()
    print(_c("1", "Audit chain: ") + (_c("32", f"intact, {verdict['count']} signed records")
                                      if verdict["ok"] else _c("31", f"BROKEN at #{verdict['broken_at']}")))
    print(f"Human queue: {len(rt.gateway.escalations.tickets)} ticket(s)\n")
    return 1 if failures else 0


def cmd_demo(args: argparse.Namespace) -> None:
    sys.exit(asyncio.run(_demo(args.only, args.verbose)))


def cmd_verify(args: argparse.Namespace) -> None:
    from .app import build_runtime

    v = build_runtime().gateway.audit.verify()
    print(v)
    sys.exit(0 if v["ok"] else 1)


def main() -> None:
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
    d.set_defaults(func=cmd_demo)
    v = sub.add_parser("verify-audit", help="Verify the audit hash chain and signatures.")
    v.set_defaults(func=cmd_verify)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
