"""Tamper-evident audit log and signed receipts.

Every gateway decision is appended to a JSONL file as a hash chain: each record contains the hash
of the previous one, and the gateway signs each record hash with its Ed25519 key. Editing,
deleting or reordering any record breaks verification from that point on.

For completed actions the gateway also returns a signed **receipt** to the calling agent, so both
sides hold the same proof of what was asked and what was agreed.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

from .identity import KeyPair, b64u, b64u_decode, canonical_json, jws_parse, jws_sign, jws_verify

GENESIS = "0" * 64
RECEIPT_TYP = "dwarpal-receipt+jwt"


def _record_hash(record: dict[str, Any]) -> str:
    body = {k: v for k, v in record.items() if k not in ("hash", "sig")}
    return hashlib.sha256(canonical_json(body)).hexdigest()


class AuditLog:
    def __init__(self, path: Path, signer: KeyPair):
        self.path = path
        self.signer = signer
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._last_hash, self._seq = GENESIS, 0
        for rec in self._read_all():
            self._last_hash, self._seq = rec.get("hash", GENESIS), rec.get("seq", self._seq)

    def _read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    out.append({"_corrupt": line})
        return out

    def append(self, event: str, **data: Any) -> dict[str, Any]:
        with self._lock:
            record = {"seq": self._seq + 1, "ts": round(time.time(), 3), "event": event,
                      "data": data, "prev": self._last_hash}
            record["hash"] = _record_hash(record)
            record["sig"] = b64u(self.signer.sign(record["hash"].encode()))
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            self._last_hash, self._seq = record["hash"], record["seq"]
            return record

    def records(self, limit: int | None = None) -> list[dict[str, Any]]:
        rows = self._read_all()
        return rows[-limit:] if limit else rows

    def verify(self) -> dict[str, Any]:
        prev = GENESIS
        rows = self._read_all()
        for i, rec in enumerate(rows, start=1):
            if "_corrupt" in rec:
                return {"ok": False, "count": len(rows), "broken_at": i, "reason": "unparseable record"}
            if rec.get("prev") != prev:
                return {"ok": False, "count": len(rows), "broken_at": rec.get("seq", i),
                        "reason": "chain broken: record does not point at the previous hash (deleted or reordered)"}
            if _record_hash(rec) != rec.get("hash"):
                return {"ok": False, "count": len(rows), "broken_at": rec.get("seq", i),
                        "reason": "content changed after it was written"}
            try:
                self.signer.public_key.verify(b64u_decode(rec["sig"]), rec["hash"].encode())
            except Exception:  # noqa: BLE001
                return {"ok": False, "count": len(rows), "broken_at": rec.get("seq", i),
                        "reason": "gateway signature invalid"}
            prev = rec["hash"]
        return {"ok": True, "count": len(rows), "head": prev}

    def tamper_for_demo(self, seq: int | None = None) -> int | None:
        """Silently edit one record (as an insider would) so the demo can show detection."""
        with self._lock:
            rows = self._read_all()
            if not rows:
                return None
            target = next((r for r in rows if r.get("seq") == seq), None) if seq else None
            target = target or next((r for r in reversed(rows) if r.get("data", {}).get("outcome") == "completed"),
                                    rows[len(rows) // 2])
            d = target.setdefault("data", {})
            d["outcome"] = "denied" if d.get("outcome") == "completed" else "completed"
            d["tampered_note"] = "edited after the fact"
            self.path.write_text("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows),
                                 encoding="utf-8")
            return target.get("seq")

    def reset(self) -> None:
        with self._lock:
            self.path.write_text("", encoding="utf-8")
            self._last_hash, self._seq = GENESIS, 0

    # ------------------------------------------------------------------ receipts

    def issue_receipt(self, claims: dict[str, Any]) -> str:
        return jws_sign(self.signer, {**claims, "iat": int(time.time()), "audit_head": self._last_hash},
                        RECEIPT_TYP)

    def verify_receipt(self, token: str) -> dict[str, Any]:
        header, claims, sig, signing_input = jws_parse(token)
        if header.get("typ") != RECEIPT_TYP:
            raise ValueError("not a Dwarpal receipt")
        jws_verify(self.signer.public_key, sig, signing_input)
        return claims
