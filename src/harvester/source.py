"""The plugin interface: subclass ``Source`` to teach harvester about a website."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any, ClassVar, Literal

from harvester.models import DataLicense, Record, Request
from harvester.page import Page

ParseResult = Iterable[Request | Record]
RobotsUnavailable = Literal["disallow", "allow"]


class Source:
    """Describes one data source: where to start and how to parse what comes back.

    Minimal example::

        class Quotes(Source):
            name = "quotes"
            start_urls = ("https://quotes.toscrape.com/",)

            def parse(self, page):
                for q in page.css(".quote"):
                    yield page.record("quote", q.css(".text::text").get(), author=...)
                if next_href := page.css("li.next a::attr(href)").get():
                    yield page.follow(next_href)

    Rules for callbacks (``parse`` and any other method named in
    ``Request.callback``): they receive a :class:`Page`, yield ``Request`` and
    ``Record`` objects, and must not do I/O of their own. Purity is what lets
    ``harvester reparse`` replay a crawl offline from the raw store.
    """

    #: Unique, CLI-friendly identifier, e.g. ``"india-code"``.
    name: ClassVar[str] = ""
    #: One line shown in ``harvester list``.
    description: ClassVar[str] = ""
    #: Terms the *harvested data* is available under (not the code's licence).
    license: ClassVar[DataLicense | None] = None
    #: Seed URLs; override :meth:`start` for anything more dynamic.
    start_urls: ClassVar[tuple[str, ...]] = ()
    #: If set, requests to any other host are dropped.
    allowed_domains: ClassVar[tuple[str, ...]] = ()
    #: Bump whenever parsing output changes, so records say which parser made them.
    parser_version: ClassVar[str] = "1"

    # ── Politeness overrides (None = use the CLI / engine defaults) ──────
    #: Minimum seconds between requests to one host.
    delay: ClassVar[float | None] = None
    #: Maximum simultaneous requests to one host.
    concurrency: ClassVar[int | None] = None
    #: What to do when robots.txt cannot be fetched (5xx / network error).
    #: RFC 9309 says assume complete disallow; "allow" requires a reason.
    robots_unavailable: ClassVar[RobotsUnavailable] = "disallow"
    robots_unavailable_reason: ClassVar[str] = ""

    def __init__(self, **options: Any) -> None:
        unknown = set(options) - set(self.option_defaults())
        if unknown:
            raise ValueError(
                f"{self.name}: unknown option(s) {sorted(unknown)}; "
                f"available: {sorted(self.option_defaults()) or 'none'}"
            )
        self.options: dict[str, Any] = {**self.option_defaults(), **options}

    @classmethod
    def option_defaults(cls) -> dict[str, Any]:
        """Options accepted via ``harvester run SOURCE -o key=value``."""
        return {}

    def start(self) -> Iterator[Request]:
        for url in self.start_urls:
            yield Request(url=url)

    def parse(self, page: Page) -> ParseResult:
        raise NotImplementedError(f"{type(self).__name__}.parse is not implemented")
