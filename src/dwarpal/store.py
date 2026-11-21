"""Durable state for what must survive a restart: human tickets, step-up approvals, lane codes and seen
request nonces. SQLite, one file per business. Session counters (turns, loops) are deliberately
in-memory; the audit chain is its own signed JSONL file.

For a multi-instance deployment, swap this for a shared store (Cloud SQL, Firestore, Redis) behind the
same four methods.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS kv (coll TEXT, id TEXT, doc TEXT, PRIMARY KEY (coll, id))")
        self.db.execute("CREATE TABLE IF NOT EXISTS nonces (nonce TEXT PRIMARY KEY, ts REAL)")

    def put(self, coll: str, id: str, doc: dict[str, Any]) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?, ?)", (coll, id, json.dumps(doc, default=str)))

    def all(self, coll: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute("SELECT doc FROM kv WHERE coll = ?", (coll,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def clear(self, coll: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM kv WHERE coll = ?", (coll,))

    def seen_nonce(self, nonce: str, now: float, window: float) -> bool:
        """True if this nonce was already used inside the window; otherwise records it."""
        with self._lock:
            self.db.execute("DELETE FROM nonces WHERE ts < ?", (now - window,))
            try:
                self.db.execute("INSERT INTO nonces VALUES (?, ?)", (nonce, now))
                return False
            except sqlite3.IntegrityError:
                return True

    def clear_nonces(self) -> None:
        with self._lock:
            self.db.execute("DELETE FROM nonces")
