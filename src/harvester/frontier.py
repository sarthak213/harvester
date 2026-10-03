"""Persistent crawl frontier: the queue of requests, their states and retry schedule.

Backed by SQLite so a crawl survives Ctrl+C, crashes and reboots. Requests are
keyed by URL fingerprint, which also gives de-duplication for free.

States: pending → inflight → done | failed | skipped
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from harvester.models import Request

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    fp          TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    priority    INTEGER NOT NULL,
    state       TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    not_before  REAL NOT NULL DEFAULT 0,
    reason      TEXT,
    seq         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS requests_ready ON requests(state, priority DESC, seq);
"""


@dataclass(frozen=True)
class Claimed:
    request: Request
    attempts: int


class Frontier:
    def __init__(self, path: Path | str) -> None:
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._lock = threading.Lock()
        with self._lock:
            row = self._db.execute("SELECT COALESCE(MAX(seq), 0) FROM requests").fetchone()
        self._seq = int(row[0])

    def close(self) -> None:
        self._db.close()

    def recover(self) -> int:
        """Return requests that were in flight when the last run stopped to the queue."""
        with self._lock, self._db:
            cur = self._db.execute("UPDATE requests SET state='pending' WHERE state='inflight'")
        return cur.rowcount

    def reset(self) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM requests")
        self._seq = 0

    def add(self, request: Request) -> bool:
        """Queue a request. Returns False if its URL was already seen."""
        with self._lock, self._db:
            self._seq += 1
            if request.dont_filter:
                self._db.execute(
                    "INSERT OR REPLACE INTO requests (fp, payload, priority, state, seq) "
                    "VALUES (?, ?, ?, 'pending', ?)",
                    (request.fingerprint, request.model_dump_json(), request.priority, self._seq),
                )
                return True
            cur = self._db.execute(
                "INSERT OR IGNORE INTO requests (fp, payload, priority, state, seq) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (request.fingerprint, request.model_dump_json(), request.priority, self._seq),
            )
            return cur.rowcount == 1

    def claim(self) -> Claimed | None:
        """Take the highest-priority request that is ready now."""
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT fp, payload, attempts FROM requests "
                "WHERE state='pending' AND not_before <= ? "
                "ORDER BY priority DESC, seq LIMIT 1",
                (time.time(),),
            ).fetchone()
            if row is None:
                return None
            self._db.execute("UPDATE requests SET state='inflight' WHERE fp=?", (row[0],))
        return Claimed(Request.model_validate_json(row[1]), int(row[2]))

    def next_ready_in(self) -> float | None:
        """Seconds until the earliest delayed request becomes ready (None if none pending)."""
        with self._lock:
            row = self._db.execute(
                "SELECT MIN(not_before) FROM requests WHERE state='pending'"
            ).fetchone()
        if row[0] is None:
            return None
        return max(0.0, float(row[0]) - time.time())

    def done(self, fp: str) -> None:
        self._set(fp, "done", None)

    def skip(self, fp: str, reason: str) -> None:
        self._set(fp, "skipped", reason)

    def fail(self, fp: str, reason: str) -> None:
        self._set(fp, "failed", reason)

    def retry(self, fp: str, delay: float, reason: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE requests SET state='pending', attempts=attempts+1, "
                "not_before=?, reason=? WHERE fp=?",
                (time.time() + delay, reason, fp),
            )

    def _set(self, fp: str, state: str, reason: str | None) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE requests SET state=?, reason=? WHERE fp=?", (state, reason, fp)
            )

    def counts(self) -> Counter[str]:
        with self._lock:
            rows = self._db.execute("SELECT state, COUNT(*) FROM requests GROUP BY state")
            return Counter({state: n for state, n in rows})

    def problems(self, limit: int = 20) -> list[tuple[str, str, str]]:
        """(state, url, reason) for failed and skipped requests."""
        with self._lock:
            rows = self._db.execute(
                "SELECT state, payload, reason FROM requests "
                "WHERE state IN ('failed','skipped') ORDER BY seq LIMIT ?",
                (limit,),
            ).fetchall()
        return [(s, Request.model_validate_json(p).url, r or "") for s, p, r in rows]
