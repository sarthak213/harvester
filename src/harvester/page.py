"""The object a parse callback receives: a fetched (or cached) response."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property
from typing import TYPE_CHECKING, Any

from w3lib.encoding import html_to_unicode

from harvester.models import CachePolicy, Record, Request

if TYPE_CHECKING:
    from scrapling.parser import Selector, Selectors


@dataclass(frozen=True)
class Page:
    """A response handed to a source's parse callback.

    Callbacks must be pure functions of the page: everything they need is the
    body, headers and ``request.meta``. That is what makes offline re-parsing
    (``harvester reparse``) produce exactly what a live run would.
    """

    url: str
    request: Request
    status: int
    headers: dict[str, str]
    body: bytes
    sha256: str
    fetched_at: datetime
    from_cache: bool = False
    _extra: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    # ── Convenience accessors ───────────────────────────────────────────

    @property
    def meta(self) -> dict[str, Any]:
        return self.request.meta

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    @cached_property
    def text(self) -> str:
        """Body decoded using the declared charset, a BOM, or detection."""
        _, text = html_to_unicode(self.headers.get("content-type"), self.body)
        return text

    def json(self) -> Any:
        return json.loads(self.body)

    @cached_property
    def selector(self) -> Selector:
        from scrapling.parser import Selector

        return Selector(content=self.body, url=self.url)

    def css(self, query: str) -> Selectors:
        return self.selector.css(query)

    def xpath(self, query: str) -> Selectors:
        return self.selector.xpath(query)

    def urljoin(self, url: str) -> str:
        from urllib.parse import urljoin

        return urljoin(self.url, url)

    # ── Producing output ────────────────────────────────────────────────

    def follow(
        self,
        url: str,
        callback: str | None = None,
        *,
        meta: dict[str, Any] | None = None,
        priority: int | None = None,
        cache: CachePolicy | None = None,
        headers: dict[str, str] | None = None,
    ) -> Request:
        """A request for ``url`` (resolved against this page), inheriting meta."""
        return Request(
            url=self.urljoin(url),
            callback=callback or self.request.callback,
            meta={**self.request.meta, **(meta or {})},
            priority=self.request.priority if priority is None else priority,
            cache=cache or CachePolicy.PREFER,
            headers=headers or {},
        )

    def record(self, kind: str, id: str, **data: Any) -> Record:
        return Record(kind=kind, id=id, data=data)
