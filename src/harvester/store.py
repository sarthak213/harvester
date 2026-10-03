"""Raw store: every response body, content-addressed, plus an index of what was fetched when.

Layout::

    <root>/index.sqlite                  responses (latest per URL) + history (every fetch)
    <root>/blobs/ab/cd/abcd…ef.gz        gzip-compressed body, named by its SHA-256

Identical bodies are stored once. The history table records each time a URL
was fetched and which body it returned, so content changes are queryable.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from harvester.fetch import FetchResponse

_SCHEMA = """
CREATE TABLE IF NOT EXISTS responses (
    fp            TEXT PRIMARY KEY,
    url           TEXT NOT NULL,
    final_url     TEXT NOT NULL,
    status        INTEGER NOT NULL,
    headers       TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    size          INTEGER NOT NULL,
    fetched_at    TEXT NOT NULL,
    validated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
    fp          TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    status      INTEGER NOT NULL,
    fetched_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS history_fp ON history(fp);
"""


@dataclass(frozen=True)
class Snapshot:
    fp: str
    url: str
    final_url: str
    status: int
    headers: dict[str, str]
    sha256: str
    size: int
    fetched_at: datetime
    validated_at: datetime

    def conditional_headers(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if etag := self.headers.get("etag"):
            out["If-None-Match"] = etag
        if modified := self.headers.get("last-modified"):
            out["If-Modified-Since"] = modified
        return out


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RawStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "blobs").mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(root / "index.sqlite", check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._db.close()

    # ── Blobs ───────────────────────────────────────────────────────────

    def blob_path(self, sha256: str) -> Path:
        return self.root / "blobs" / sha256[:2] / sha256[2:4] / f"{sha256}.gz"

    def write_blob(self, body: bytes) -> str:
        sha256 = hashlib.sha256(body).hexdigest()
        path = self.blob_path(sha256)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(gzip.compress(body, compresslevel=6, mtime=0))
            tmp.replace(path)  # atomic: a crash never leaves a half-written blob
        return sha256

    def read_blob(self, sha256: str) -> bytes:
        return gzip.decompress(self.blob_path(sha256).read_bytes())

    # ── Index ───────────────────────────────────────────────────────────

    def get(self, fp: str) -> Snapshot | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM responses WHERE fp = ?", (fp,)).fetchone()
        return _snapshot(row) if row else None

    def put(self, fp: str, url: str, response: FetchResponse) -> Snapshot:
        sha256 = self.write_blob(response.body)
        now = _now().isoformat()
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO responses VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    fp,
                    url,
                    response.url,
                    response.status,
                    json.dumps(response.headers),
                    sha256,
                    len(response.body),
                    now,
                    now,
                ),
            )
            self._db.execute(
                "INSERT INTO history VALUES (?,?,?,?)", (fp, sha256, response.status, now)
            )
        snapshot = self.get(fp)
        assert snapshot is not None
        return snapshot

    def mark_validated(self, fp: str) -> None:
        """Record a 304 Not Modified: the stored body is still current."""
        with self._lock, self._db:
            self._db.execute(
                "UPDATE responses SET validated_at = ? WHERE fp = ?", (_now().isoformat(), fp)
            )

    def __iter__(self) -> Iterator[Snapshot]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM responses ORDER BY fetched_at").fetchall()
        for row in rows:
            yield _snapshot(row)

    def changed(self) -> list[tuple[str, int]]:
        """URLs whose body has changed across fetches, with the number of versions."""
        with self._lock:
            rows = self._db.execute(
                """SELECT r.url, COUNT(DISTINCT h.sha256) FROM history h
                   JOIN responses r ON r.fp = h.fp
                   GROUP BY h.fp HAVING COUNT(DISTINCT h.sha256) > 1"""
            ).fetchall()
        return [(url, versions) for url, versions in rows]

    def stats(self) -> dict[str, int]:
        with self._lock:
            responses, size = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM responses"
            ).fetchone()
            blobs = self._db.execute("SELECT COUNT(DISTINCT sha256) FROM responses").fetchone()[0]
        return {"responses": responses, "unique_bodies": blobs, "bytes": size}


def _snapshot(row: tuple[object, ...]) -> Snapshot:
    fp, url, final_url, status, headers, sha256, size, fetched_at, validated_at = row
    return Snapshot(
        fp=str(fp),
        url=str(url),
        final_url=str(final_url),
        status=int(status),  # type: ignore[call-overload]
        headers=json.loads(str(headers)),
        sha256=str(sha256),
        size=int(size),  # type: ignore[call-overload]
        fetched_at=datetime.fromisoformat(str(fetched_at)),
        validated_at=datetime.fromisoformat(str(validated_at)),
    )
