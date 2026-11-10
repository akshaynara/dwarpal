"""Outbound data guard: what the business agent says back is filtered before it leaves.

Defense in depth. Even if a request somehow steered the business agent, fields the policy says
must never reach another agent are dropped, and PII patterns in text are redacted.
"""

from __future__ import annotations

from typing import Any

from .policy import DataPolicy


def filter_outbound(value: Any, policy: DataPolicy, path: str = "") -> tuple[Any, list[str]]:
    redactions: list[str] = []

    def walk(v: Any, p: str) -> Any:
        if isinstance(v, dict):
            out = {}
            for k, sub in v.items():
                kp = f"{p}.{k}" if p else k
                if k.lower() in policy.never_disclose_fields:
                    redactions.append(f"dropped field '{kp}'")
                    continue
                out[k] = walk(sub, kp)
            return out
        if isinstance(v, list):
            return [walk(x, f"{p}[{i}]") for i, x in enumerate(v)]
        if isinstance(v, str):
            s = v
            for name, pattern in policy.redact_patterns.items():
                s, n = pattern.subn(f"[redacted:{name}]", s)
                if n:
                    redactions.append(f"redacted {n}× {name} in '{p or 'text'}'")
            return s
        return v

    return walk(value, path), redactions
