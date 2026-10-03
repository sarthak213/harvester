"""Focused tests for the building blocks."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from itertools import pairwise
from pathlib import Path

import pytest

from harvester.fetch import FetchError, FetchResponse
from harvester.frontier import Frontier
from harvester.models import Request, url_fingerprint
from harvester.politeness import HostThrottle, RobotsPolicy, parse_retry_after
from harvester.store import RawStore


def response(body: bytes, status: int = 200, **headers: str) -> FetchResponse:
    return FetchResponse("http://x/", status, headers, body, 0.01)


# ── URL fingerprints ──────────────────────────────────────────────────


def test_fingerprint_ignores_query_order_and_fragments() -> None:
    assert url_fingerprint("http://e.com/a?b=2&a=1#top") == url_fingerprint(
        "http://e.com/a?a=1&b=2"
    )
    assert url_fingerprint("http://e.com/a") != url_fingerprint("http://e.com/b")


# ── Frontier ──────────────────────────────────────────────────────────


def test_frontier_dedupes_and_orders_by_priority(tmp_path: Path) -> None:
    f = Frontier(tmp_path / "f.sqlite")
    assert f.add(Request(url="http://e.com/low"))
    assert f.add(Request(url="http://e.com/high", priority=5))
    assert not f.add(Request(url="http://e.com/low"))
    claimed = f.claim()
    assert claimed is not None and claimed.request.url.endswith("/high")
    f.close()


def test_frontier_recovers_inflight_after_crash(tmp_path: Path) -> None:
    f = Frontier(tmp_path / "f.sqlite")
    f.add(Request(url="http://e.com/a"))
    assert f.claim() is not None
    f.close()  # "crash" with the request in flight

    f = Frontier(tmp_path / "f.sqlite")
    assert f.recover() == 1
    assert f.counts()["pending"] == 1
    f.close()


def test_frontier_delays_retries(tmp_path: Path) -> None:
    f = Frontier(tmp_path / "f.sqlite")
    f.add(Request(url="http://e.com/a"))
    claimed = f.claim()
    assert claimed is not None
    f.retry(claimed.request.fingerprint, delay=60, reason="503")
    assert f.claim() is None
    wait = f.next_ready_in()
    assert wait is not None and 55 < wait <= 60
    f.close()


# ── Raw store ─────────────────────────────────────────────────────────


def test_store_dedupes_bodies_and_tracks_changes(tmp_path: Path) -> None:
    store = RawStore(tmp_path / "store")
    store.put("fp1", "http://e.com/1", response(b"same"))
    store.put("fp2", "http://e.com/2", response(b"same"))
    assert store.stats()["unique_bodies"] == 1

    store.put("fp1", "http://e.com/1", response(b"changed"))
    assert store.changed() == [("http://e.com/1", 2)]
    snapshot = store.get("fp1")
    assert snapshot is not None and store.read_blob(snapshot.sha256) == b"changed"
    store.close()


def test_snapshot_builds_conditional_headers(tmp_path: Path) -> None:
    store = RawStore(tmp_path / "store")
    snap = store.put("fp", "http://e.com/", response(b"x", etag='"v1"', **{"last-modified": "Mon"}))
    assert snap.conditional_headers() == {"If-None-Match": '"v1"', "If-Modified-Since": "Mon"}
    store.close()


# ── Politeness ────────────────────────────────────────────────────────


def test_retry_after_parses_seconds_and_dates() -> None:
    assert parse_retry_after("120") == 120.0
    later = datetime.now(timezone.utc) + timedelta(seconds=90)
    parsed = parse_retry_after(format_datetime(later, usegmt=True))
    assert parsed is not None and 85 <= parsed <= 91
    assert parse_retry_after("garbage") is None
    assert parse_retry_after(None) is None


def robots_policy(result: FetchResponse | Exception, unavailable: str = "disallow") -> RobotsPolicy:
    async def fetch(url: str) -> FetchResponse:
        if isinstance(result, Exception):
            raise result
        return result

    return RobotsPolicy(fetch, "harvester/0.1 (+x)", unavailable)


async def test_robots_rules_and_crawl_delay() -> None:
    body = b"User-agent: *\nCrawl-delay: 3\nDisallow: /admin\n"
    policy = robots_policy(response(body))
    allowed = await policy.check("http://e.com/page")
    blocked = await policy.check("http://e.com/admin/x")
    assert allowed.allowed and allowed.crawl_delay == 3.0
    assert not blocked.allowed


async def test_robots_agent_specific_group() -> None:
    body = b"User-agent: harvester\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
    assert not (await robots_policy(response(body)).check("http://e.com/x")).allowed


@pytest.mark.parametrize(
    ("result", "allowed"),
    [
        (response(b"", status=404), True),
        (response(b"", status=500), False),
        (FetchError("dns"), False),
    ],
)
async def test_robots_rfc9309_status_handling(
    result: FetchResponse | Exception, allowed: bool
) -> None:
    decision = await robots_policy(result).check("http://e.com/x")
    assert decision.allowed is allowed


async def test_robots_unreachable_can_be_allowed_explicitly() -> None:
    decision = await robots_policy(response(b"", status=503), "allow").check("http://e.com/x")
    assert decision.allowed and "allowed by source policy" in decision.reason


async def test_throttle_spaces_requests_per_host() -> None:
    throttle = HostThrottle(delay=0.2, concurrency=4)
    starts: list[float] = []

    async def hit() -> None:
        async with throttle.slot("e.com"):
            starts.append(time.monotonic())

    await asyncio.gather(*(hit() for _ in range(3)))
    gaps = [b - a for a, b in pairwise(starts)]
    assert all(g >= 0.18 for g in gaps)


def test_throttle_penalize_and_recover() -> None:
    throttle = HostThrottle(delay=1.0)
    wait = throttle.penalize("e.com", retry_after=10)
    assert wait == 10 and throttle.delay_for("e.com") == 2.0
    for _ in range(50):
        throttle.reward("e.com")
    assert throttle.delay_for("e.com") == 1.0
    throttle.set_minimum_delay("e.com", 5)
    assert throttle.delay_for("e.com") == 5
