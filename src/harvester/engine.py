"""The crawl engine: frontier → politeness → fetch (or cache) → parse → sink."""

from __future__ import annotations

import asyncio
import logging
import random
import traceback
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from harvester import __version__
from harvester.fetch import (
    DEFAULT_USER_AGENT,
    Fetcher,
    FetchError,
    FetchResponse,
    OfflineFetcher,
    ScraplingFetcher,
)
from harvester.frontier import Claimed, Frontier
from harvester.models import CachePolicy, Provenance, Record, Request
from harvester.page import Page
from harvester.politeness import HostThrottle, RobotsPolicy, host_of, parse_retry_after
from harvester.sinks import JsonlSink, write_manifest
from harvester.source import Source
from harvester.store import RawStore, Snapshot

log = logging.getLogger("harvester")

Mode = Literal["crawl", "reparse"]
Outcome = Literal["complete", "limit", "interrupted"]


@dataclass
class Settings:
    data_dir: Path = Path(".harvester")
    concurrency: int = 4
    delay: float = 1.0
    host_concurrency: int = 1
    user_agent: str = DEFAULT_USER_AGENT
    timeout: float = 60.0
    max_attempts: int = 4
    limit: int | None = None
    refresh: bool = False
    restart: bool = False
    robots: bool = True
    robots_unavailable: str | None = None


@dataclass
class Stats:
    pages: int = 0
    fetched: int = 0
    from_cache: int = 0
    not_modified: int = 0
    records: int = 0
    queued: int = 0
    retries: int = 0
    failed: int = 0
    robots_blocked: int = 0
    offsite: int = 0
    missing: int = 0
    bytes: int = 0
    recent_errors: deque[str] = field(default_factory=lambda: deque(maxlen=8))

    def as_dict(self) -> dict[str, int]:
        return {k: v for k, v in asdict(self).items() if isinstance(v, int)}


@dataclass(frozen=True)
class RunReport:
    run_id: str
    mode: Mode
    outcome: Outcome
    run_dir: Path
    records_path: Path
    stats: dict[str, int]
    records_by_kind: dict[str, int]
    frontier: dict[str, int]


class Harvester:
    """Runs one source. Construct, then ``await run()`` (or ``reparse()``)."""

    def __init__(
        self,
        source: Source,
        settings: Settings | None = None,
        *,
        fetcher: Fetcher | None = None,
    ) -> None:
        self.source = source
        self.settings = settings or Settings()
        self.root = self.settings.data_dir / source.name
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = RawStore(self.root / "store")
        self.stats = Stats()
        self._fetcher = fetcher
        self._stop = asyncio.Event()
        self._inflight = 0
        self._mode: Mode = "crawl"
        self.frontier: Frontier | None = None

    # ── Public API ──────────────────────────────────────────────────────

    async def run(self) -> RunReport:
        """Crawl: resume unfinished work, or start a new pass over the source."""
        frontier = Frontier(self.root / "frontier.sqlite")
        recovered = frontier.recover()
        counts = frontier.counts()
        if self.settings.restart or not (counts["pending"] or counts["inflight"]):
            frontier.reset()  # new pass; the raw store still acts as a cache
        elif recovered:
            log.info("resuming: %d in-flight requests returned to the queue", recovered)
        fetcher = self._fetcher or ScraplingFetcher(self.settings.user_agent, self.settings.timeout)
        return await self._execute("crawl", frontier, fetcher)

    async def reparse(self) -> RunReport:
        """Replay the crawl graph from the raw store with zero network access."""
        return await self._execute("reparse", Frontier(":memory:"), OfflineFetcher())

    def stop(self) -> None:
        """Finish in-flight requests, then stop. Progress is kept for the next run."""
        self._stop.set()

    def close(self) -> None:
        self.store.close()

    # ── Orchestration ───────────────────────────────────────────────────

    async def _execute(self, mode: Mode, frontier: Frontier, fetcher: Fetcher) -> RunReport:
        self._mode = mode
        self.frontier = frontier
        self._fetcher_in_use = fetcher
        source = self.source
        settings = self.settings

        self._throttle = HostThrottle(
            delay=source.delay if source.delay is not None else settings.delay,
            concurrency=source.concurrency or settings.host_concurrency,
        )
        unavailable = settings.robots_unavailable or source.robots_unavailable
        self._robots = RobotsPolicy(self._fetch_robots, settings.user_agent, unavailable)

        started = datetime.now(timezone.utc)
        run_id, run_dir = self._new_run_dir(started, mode)
        self._sink = JsonlSink(run_dir)

        for request in source.start():
            if frontier.add(request):
                self.stats.queued += 1

        outcome: Outcome = "interrupted"
        try:
            workers = max(1, settings.concurrency)
            await asyncio.gather(*(self._worker() for _ in range(workers)))
            limit_hit = settings.limit is not None and self.stats.pages >= settings.limit
            outcome = "interrupted" if self._stop.is_set() else "limit" if limit_hit else "complete"
        finally:
            self._sink.close()
            if mode == "crawl":
                await fetcher.close()
            frontier_counts = dict(frontier.counts())
            manifest = self._manifest(run_id, mode, outcome, started, frontier_counts)
            write_manifest(run_dir, manifest)
            frontier.close()
            self.frontier = None

        return RunReport(
            run_id=run_id,
            mode=mode,
            outcome=outcome,
            run_dir=run_dir,
            records_path=self._sink.path,
            stats=self.stats.as_dict(),
            records_by_kind=dict(self._sink.counts),
            frontier=frontier_counts,
        )

    async def _worker(self) -> None:
        assert self.frontier is not None
        limit = self.settings.limit
        while not self._stop.is_set():
            if limit is not None and self.stats.pages + self._inflight >= limit:
                return
            claimed = self.frontier.claim()
            if claimed is None:
                if self._inflight == 0:
                    wait = self.frontier.next_ready_in()
                    if wait is None:
                        return  # nothing pending and nothing in flight: done
                    await asyncio.sleep(min(wait, 1.0))
                else:
                    await asyncio.sleep(0.05)  # others may still discover new URLs
                continue
            self._inflight += 1
            try:
                await self._process(claimed)
            except Exception as exc:  # a bug in harvester itself; never kill the crawl
                self._fail(claimed.request, f"internal error: {exc!r}")
                log.exception("internal error processing %s", claimed.request.url)
            finally:
                self._inflight -= 1

    # ── One request ─────────────────────────────────────────────────────

    async def _process(self, claimed: Claimed) -> None:
        assert self.frontier is not None
        request = claimed.request
        fp = request.fingerprint
        host = host_of(request.url)

        if not self._in_scope(host):
            self.stats.offsite += 1
            self.frontier.skip(fp, "outside allowed_domains")
            return

        snapshot = self.store.get(fp)
        policy = request.cache
        if self.settings.refresh and policy is CachePolicy.PREFER:
            policy = CachePolicy.REVALIDATE

        page: Page | None = None
        if snapshot and (policy is CachePolicy.PREFER or self._mode == "reparse"):
            page = self._page(snapshot, request, from_cache=True)
            self.stats.from_cache += 1
        elif self._mode == "reparse":
            self.stats.missing += 1
            self.frontier.skip(fp, "not in raw store")
            return
        else:
            page = await self._fetch_page(claimed, snapshot, policy, host)
            if page is None:
                return

        if not 200 <= page.status < 300:
            self._fail(request, f"HTTP {page.status}")
            return
        self._run_callback(page)

    async def _fetch_page(
        self, claimed: Claimed, snapshot: Snapshot | None, policy: CachePolicy, host: str
    ) -> Page | None:
        assert self.frontier is not None
        request = claimed.request
        fp = request.fingerprint

        if self.settings.robots:
            decision = await self._robots.check(request.url)
            if not decision.allowed:
                self.stats.robots_blocked += 1
                self.frontier.skip(fp, decision.reason)
                return None
            if decision.crawl_delay:
                self._throttle.set_minimum_delay(host, decision.crawl_delay)

        headers = dict(request.headers)
        if snapshot and policy is CachePolicy.REVALIDATE:
            headers.update(snapshot.conditional_headers())

        try:
            async with self._throttle.slot(host):
                response = await self._fetcher_in_use.fetch(request.url, headers)
        except FetchError as exc:
            self._retry_or_fail(claimed, f"network error: {exc}", self._backoff(claimed.attempts))
            return None

        if response.status in (429, 503):
            retry_after = parse_retry_after(response.headers.get("retry-after"))
            wait = self._throttle.penalize(host, retry_after)
            self._retry_or_fail(claimed, f"HTTP {response.status} (backing off {wait:.0f}s)", wait)
            return None
        if response.status >= 500:
            self._retry_or_fail(claimed, f"HTTP {response.status}", self._backoff(claimed.attempts))
            return None
        self._throttle.reward(host)

        if response.status == 304:
            if snapshot is None:
                self._fail(request, "HTTP 304 without a stored copy")
                return None
            self.store.mark_validated(fp)
            self.stats.not_modified += 1
            return self._page(snapshot, request, from_cache=True)

        snapshot = self.store.put(fp, request.url, response)
        self.stats.fetched += 1
        self.stats.bytes += len(response.body)
        return self._page(snapshot, request, from_cache=False, body=response.body)

    def _run_callback(self, page: Page) -> None:
        assert self.frontier is not None
        request = page.request
        callback = getattr(self.source, request.callback, None)
        if not callable(callback):
            self._fail(request, f"source has no callback {request.callback!r}")
            return
        provenance = Provenance(
            source=self.source.name,
            url=page.url,
            fetched_at=page.fetched_at,
            sha256=page.sha256,
            status=page.status,
            parser_version=self.source.parser_version,
            harvester_version=__version__,
            license=self.source.license,
        )
        try:
            outputs = list(callback(page) or ())
        except Exception as exc:
            where = traceback.extract_tb(exc.__traceback__)[-1]
            self._fail(request, f"parse error in {request.callback}: {exc!r} (line {where.lineno})")
            return
        for item in outputs:
            if isinstance(item, Request):
                if self.frontier.add(item):
                    self.stats.queued += 1
            elif isinstance(item, Record):
                self._sink.write(item, provenance)
                self.stats.records += 1
            else:
                self._fail(request, f"{request.callback} yielded {type(item).__name__}")
                return
        self.stats.pages += 1
        self.frontier.done(request.fingerprint)

    # ── Helpers ─────────────────────────────────────────────────────────

    def _new_run_dir(self, started: datetime, mode: Mode) -> tuple[str, Path]:
        base = started.strftime("%Y%m%dT%H%M%SZ") + ("-reparse" if mode == "reparse" else "")
        runs = self.root / "runs"
        run_id, n = base, 1
        while (runs / run_id).exists():
            n += 1
            run_id = f"{base}-{n}"
        return run_id, runs / run_id

    async def _fetch_robots(self, url: str) -> FetchResponse:
        async with self._throttle.slot(host_of(url)):
            return await self._fetcher_in_use.fetch(url, {})

    def _in_scope(self, host: str) -> bool:
        allowed = self.source.allowed_domains
        return not allowed or any(host == d or host.endswith("." + d) for d in allowed)

    def _page(
        self, snapshot: Snapshot, request: Request, *, from_cache: bool, body: bytes | None = None
    ) -> Page:
        return Page(
            url=snapshot.final_url,
            request=request,
            status=snapshot.status,
            headers=snapshot.headers,
            body=body if body is not None else self.store.read_blob(snapshot.sha256),
            sha256=snapshot.sha256,
            fetched_at=snapshot.fetched_at,
            from_cache=from_cache,
        )

    def _backoff(self, attempts: int) -> float:
        return float(min(300.0, 2.0 * 2**attempts + random.uniform(0, 1)))

    def _retry_or_fail(self, claimed: Claimed, reason: str, delay: float) -> None:
        assert self.frontier is not None
        if claimed.attempts + 1 >= self.settings.max_attempts:
            self._fail(claimed.request, f"{reason}; gave up after {claimed.attempts + 1} attempts")
            return
        self.stats.retries += 1
        self.frontier.retry(claimed.request.fingerprint, delay, reason)

    def _fail(self, request: Request, reason: str) -> None:
        assert self.frontier is not None
        self.stats.failed += 1
        self.stats.recent_errors.append(f"{reason} - {request.url}")
        self.frontier.fail(request.fingerprint, reason)
        log.warning("%s: %s", request.url, reason)

    def _manifest(
        self,
        run_id: str,
        mode: Mode,
        outcome: Outcome,
        started: datetime,
        frontier_counts: dict[str, int],
    ) -> dict[str, Any]:
        finished = datetime.now(timezone.utc)
        source = self.source
        return {
            "run_id": run_id,
            "mode": mode,
            "outcome": outcome,
            "source": {
                "name": source.name,
                "class": f"{type(source).__module__}.{type(source).__qualname__}",
                "options": source.options,
                "parser_version": source.parser_version,
                "license": source.license.model_dump() if source.license else None,
                "robots_unavailable": self.settings.robots_unavailable or source.robots_unavailable,
                "robots_unavailable_reason": source.robots_unavailable_reason or None,
            },
            "harvester_version": __version__,
            "settings": {
                "concurrency": self.settings.concurrency,
                "delay": source.delay if source.delay is not None else self.settings.delay,
                "host_concurrency": source.concurrency or self.settings.host_concurrency,
                "user_agent": self.settings.user_agent,
                "refresh": self.settings.refresh,
                "limit": self.settings.limit,
                "robots": self.settings.robots,
            },
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "duration_s": round((finished - started).total_seconds(), 3),
            "stats": self.stats.as_dict(),
            "records_by_kind": dict(self._sink.counts),
            "frontier": frontier_counts,
        }
