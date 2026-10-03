"""End-to-end behaviour of the crawl engine against a local HTTP server."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from harvester import CachePolicy, Page, Record, Request, Source
from harvester.engine import Harvester, Settings

from .conftest import Hit, Site


def make_source(site: Site, **attrs: object) -> Source:
    host = site.base.split("//")[1].split(":")[0]

    class Catalog(Source):
        name = "catalog"
        allowed_domains = (host,)

        def start(self) -> Iterator[Request]:
            yield Request(url=site.url("/"), cache=CachePolicy.REVALIDATE)

        def parse(self, page: Page) -> Iterator[Request | Record]:
            for href in page.css("a.item::attr(href)").getall():
                yield page.follow(href, callback="parse_item")
            if nxt := page.css("a.next::attr(href)").get():
                yield page.follow(nxt, cache=CachePolicy.REVALIDATE)

        def parse_item(self, page: Page) -> Iterator[Record]:
            if "explode" in page.text:
                raise ValueError("unparseable item")
            yield page.record("item", page.url.rsplit("/", 1)[-1], title=page.css("h1::text").get())

    for key, value in attrs.items():
        setattr(Catalog, key, value)
    return Catalog()


def build_site(site: Site) -> None:
    site.route(
        "/robots.txt", "User-agent: *\nDisallow: /private/\n", **{"Content-Type": "text/plain"}
    )
    site.route(
        "/",
        """<a class="item" href="/items/a">A</a>
           <a class="item" href="/items/b">B</a>
           <a class="item" href="/private/secret">S</a>
           <a class="item" href="http://elsewhere.invalid/x">X</a>
           <a class="next" href="/page/2">next</a>""",
        ETag='"home-v1"',
    )
    site.route("/page/2", '<a class="item" href="/items/c">C</a>')
    for name in "abc":
        site.route(f"/items/{name}", f"<h1>Item {name.upper()}</h1>")
    site.route("/private/secret", "<h1>Secret</h1>")


def records(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


async def test_crawl_follows_links_and_respects_robots_and_scope(
    site: Site, settings: Settings
) -> None:
    build_site(site)
    engine = Harvester(make_source(site), settings)
    report = await engine.run()
    engine.close()

    assert report.outcome == "complete"
    out = records(report.records_path)
    assert sorted(r["id"] for r in out) == ["a", "b", "c"]
    assert all(r["provenance"]["source"] == "catalog" for r in out)
    assert all(len(r["provenance"]["sha256"]) == 64 for r in out)
    assert "/private/secret" not in site.paths()  # robots.txt obeyed
    assert report.stats["robots_blocked"] == 1
    assert report.stats["offsite"] == 1
    assert all(h.headers["user-agent"] == settings.user_agent for h in site.hits)


async def test_interrupted_crawl_resumes_without_refetching(site: Site, settings: Settings) -> None:
    build_site(site)
    settings.limit = 2
    first = Harvester(make_source(site), settings)
    report = await first.run()
    first.close()
    assert report.outcome == "limit"
    fetched_first = [p for p in site.paths() if p != "/robots.txt"]

    settings.limit = None
    second = Harvester(make_source(site), settings)
    report = await second.run()
    second.close()
    assert report.outcome == "complete"
    fetched_second = [p for p in site.paths() if p != "/robots.txt"][len(fetched_first) :]
    assert not set(fetched_first) & set(fetched_second), "resumed run re-fetched pages"


async def test_new_pass_serves_documents_from_cache_and_revalidates_listings(
    site: Site, settings: Settings
) -> None:
    build_site(site)
    engine = Harvester(make_source(site), settings)
    await engine.run()
    engine.close()
    site.hits.clear()

    engine = Harvester(make_source(site), settings)
    report = await engine.run()
    engine.close()

    network = [p for p in site.paths() if p != "/robots.txt"]
    assert network == ["/", "/page/2"]  # only the REVALIDATE listings went out
    home = next(h for h in site.hits if h.path == "/")
    assert home.headers.get("if-none-match") == '"home-v1"'
    assert report.stats["from_cache"] == 3
    assert sorted(r["id"] for r in records(report.records_path)) == ["a", "b", "c"]


async def test_not_modified_reuses_stored_body(site: Site, settings: Settings) -> None:
    build_site(site)

    @site.handler("/")
    def home(hit: Hit) -> tuple[int, dict[str, str], bytes]:
        if hit.headers.get("if-none-match") == '"v1"':
            return 304, {"ETag": '"v1"'}, b""
        return (
            200,
            {"ETag": '"v1"', "Content-Type": "text/html"},
            b'<a class="item" href="/items/a">A</a>',
        )

    engine = Harvester(make_source(site), settings)
    await engine.run()
    engine.close()
    engine = Harvester(make_source(site), settings)
    report = await engine.run()
    engine.close()
    assert report.stats["not_modified"] == 1
    assert [r["id"] for r in records(report.records_path)] == ["a"]


async def test_rate_limit_backs_off_and_retries(site: Site, settings: Settings) -> None:
    build_site(site)
    calls = {"n": 0}

    @site.handler("/items/a")
    def flaky(hit: Hit) -> tuple[int, dict[str, str], bytes]:
        calls["n"] += 1
        if calls["n"] == 1:
            return 429, {"Retry-After": "0"}, b"slow down"
        return 200, {"Content-Type": "text/html"}, b"<h1>Item A</h1>"

    engine = Harvester(make_source(site), settings)
    report = await engine.run()
    engine.close()
    assert calls["n"] == 2
    assert report.stats["retries"] == 1
    assert "a" in [r["id"] for r in records(report.records_path)]


async def test_gives_up_after_max_attempts(site: Site, settings: Settings) -> None:
    build_site(site)
    site.route("/items/b", "down", status=500)
    settings.max_attempts = 2

    engine = Harvester(make_source(site), settings)
    report = await engine.run()
    engine.close()
    assert report.frontier["failed"] == 1
    assert site.paths().count("/items/b") == 2
    assert sorted(r["id"] for r in records(report.records_path)) == ["a", "c"]


async def test_parse_errors_are_isolated(site: Site, settings: Settings) -> None:
    build_site(site)
    site.route("/items/b", "<h1>explode</h1>")
    engine = Harvester(make_source(site), settings)
    report = await engine.run()
    engine.close()
    assert report.outcome == "complete"
    assert report.frontier["failed"] == 1
    assert any("unparseable item" in e for e in engine.stats.recent_errors)
    assert sorted(r["id"] for r in records(report.records_path)) == ["a", "c"]


@pytest.mark.parametrize(("policy", "expect_records"), [("disallow", 0), ("allow", 4)])
async def test_unreachable_robots_txt_follows_policy(
    site: Site, settings: Settings, policy: str, expect_records: int
) -> None:
    build_site(site)
    site.route("/robots.txt", "boom", status=500)
    source = make_source(site, robots_unavailable=policy, robots_unavailable_reason="test")
    engine = Harvester(source, settings)
    report = await engine.run()
    engine.close()
    assert report.stats["records"] == expect_records
    if policy == "disallow":
        assert site.paths() == ["/robots.txt"]


async def test_missing_robots_txt_allows_crawling(site: Site, settings: Settings) -> None:
    build_site(site)
    del site.routes["/robots.txt"]  # 404
    engine = Harvester(make_source(site), settings)
    report = await engine.run()
    engine.close()
    assert report.stats["records"] == 4  # private page is allowed now


async def test_reparse_is_offline_and_reproduces_records(site: Site, settings: Settings) -> None:
    build_site(site)
    engine = Harvester(make_source(site), settings)
    crawl = await engine.run()
    engine.close()
    site.hits.clear()

    engine = Harvester(make_source(site), settings)
    replay = await engine.reparse()
    engine.close()

    assert site.hits == []  # not a single request
    assert replay.mode == "reparse"

    def strip(rows: list[dict[str, object]]) -> list[tuple[object, object, object]]:
        return sorted((r["kind"], r["id"], json.dumps(r["data"])) for r in rows)

    assert strip(records(replay.records_path)) == strip(records(crawl.records_path))


async def test_manifest_describes_the_run(site: Site, settings: Settings) -> None:
    build_site(site)
    engine = Harvester(make_source(site), settings)
    report = await engine.run()
    engine.close()
    manifest = json.loads((report.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["outcome"] == "complete"
    assert manifest["source"]["name"] == "catalog"
    assert manifest["records_by_kind"] == {"item": 3}
    assert manifest["settings"]["user_agent"] == settings.user_agent
