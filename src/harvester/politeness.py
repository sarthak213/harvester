"""Being a good citizen: robots.txt (RFC 9309) and adaptive per-host throttling."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from protego import Protego

from harvester.fetch import FetchError, FetchResponse

#: RFC 9309 §2.5: crawlers must parse at least the first 500 KiB.
ROBOTS_MAX_BYTES = 500 * 1024


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


@dataclass(frozen=True)
class RobotsDecision:
    allowed: bool
    crawl_delay: float | None
    reason: str


@dataclass
class _RobotsEntry:
    parser: Protego | None  # None means "no parser": see `allow_all`
    allow_all: bool
    status: str


class RobotsPolicy:
    """Fetches, caches and evaluates robots.txt per origin.

    Outcomes follow RFC 9309 §2.3.1:
      * 2xx  → parse and obey the rules (and Crawl-delay, a common extension)
      * 4xx  → "unavailable": crawling is allowed
      * 5xx / network error → "unreachable": complete disallow, unless the
        source explicitly opted into ``robots_unavailable="allow"`` with a reason
    """

    def __init__(
        self,
        fetch: Callable[[str], Awaitable[FetchResponse]],
        user_agent: str,
        unavailable: str = "disallow",
    ) -> None:
        self._fetch = fetch
        self._agent = user_agent.split("/")[0]
        self._unavailable = unavailable
        self._cache: dict[str, _RobotsEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def check(self, url: str) -> RobotsDecision:
        entry = await self._entry(origin_of(url))
        if entry.parser is None:
            return RobotsDecision(entry.allow_all, None, entry.status)
        allowed = bool(entry.parser.can_fetch(url, self._agent))
        delay = entry.parser.crawl_delay(self._agent)
        return RobotsDecision(
            allowed,
            float(delay) if delay is not None else None,
            entry.status if allowed else "disallowed by robots.txt",
        )

    async def _entry(self, origin: str) -> _RobotsEntry:
        if origin in self._cache:
            return self._cache[origin]
        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin not in self._cache:
                self._cache[origin] = await self._load(origin)
        return self._cache[origin]

    async def _load(self, origin: str) -> _RobotsEntry:
        try:
            response = await self._fetch(f"{origin}/robots.txt")
        except FetchError as exc:
            return self._unreachable(f"robots.txt unreachable ({exc})")
        if 200 <= response.status < 300:
            text = response.body[:ROBOTS_MAX_BYTES].decode("utf-8", errors="replace")
            return _RobotsEntry(Protego.parse(text), allow_all=True, status="robots.txt ok")
        if 400 <= response.status < 500:
            return _RobotsEntry(None, allow_all=True, status=f"no robots.txt ({response.status})")
        return self._unreachable(f"robots.txt returned {response.status}")

    def _unreachable(self, why: str) -> _RobotsEntry:
        if self._unavailable == "allow":
            return _RobotsEntry(None, allow_all=True, status=f"{why}; allowed by source policy")
        return _RobotsEntry(None, allow_all=False, status=f"{why}; treating as disallow (RFC 9309)")


@dataclass
class _HostState:
    semaphore: asyncio.Semaphore
    delay: float
    base_delay: float
    next_slot: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class HostThrottle:
    """Per-host concurrency limit plus a minimum gap between request starts.

    The gap adapts: 429/503 responses double it (honouring Retry-After),
    and each healthy response shrinks it back toward the base delay.
    """

    def __init__(self, delay: float = 1.0, concurrency: int = 1, max_delay: float = 120.0) -> None:
        self.default_delay = delay
        self.default_concurrency = concurrency
        self.max_delay = max_delay
        self._hosts: dict[str, _HostState] = {}

    def _state(self, host: str) -> _HostState:
        if host not in self._hosts:
            self._hosts[host] = _HostState(
                semaphore=asyncio.Semaphore(self.default_concurrency),
                delay=self.default_delay,
                base_delay=self.default_delay,
            )
        return self._hosts[host]

    def delay_for(self, host: str) -> float:
        return self._state(host).delay

    def set_minimum_delay(self, host: str, seconds: float) -> None:
        """Raise the floor, e.g. to a robots.txt Crawl-delay."""
        state = self._state(host)
        state.base_delay = max(state.base_delay, seconds)
        state.delay = max(state.delay, state.base_delay)

    @asynccontextmanager
    async def slot(self, host: str) -> AsyncIterator[None]:
        state = self._state(host)
        async with state.semaphore:
            async with state.lock:
                wait = state.next_slot - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                state.next_slot = time.monotonic() + state.delay
            yield

    def penalize(self, host: str, retry_after: float | None = None) -> float:
        """Back off after an overload signal. Returns seconds to wait before retrying."""
        state = self._state(host)
        state.delay = min(self.max_delay, max(state.delay * 2, state.base_delay, 1.0))
        wait = max(retry_after or 0.0, state.delay)
        state.next_slot = max(state.next_slot, time.monotonic() + wait)
        return wait

    def reward(self, host: str) -> None:
        state = self._state(host)
        if state.delay > state.base_delay:
            state.delay = max(state.base_delay, state.delay * 0.9)


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    """Retry-After is either delta-seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        return None
    return max(0.0, when - (now if now is not None else time.time()))
