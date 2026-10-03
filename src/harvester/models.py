"""Core data types: what a source asks for (Request) and what it produces (Record)."""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from w3lib.url import canonicalize_url


class CachePolicy(str, Enum):
    """How a request treats a response already in the raw store.

    PREFER      use the stored response if there is one; never touch the network
                for it again. Right for immutable things (documents, files).
    REVALIDATE  send a conditional request (If-None-Match / If-Modified-Since);
                a 304 reuses the stored body. Right for listings that change.
    BYPASS      always fetch a fresh copy.
    """

    PREFER = "prefer"
    REVALIDATE = "revalidate"
    BYPASS = "bypass"


class DataLicense(BaseModel):
    """The terms the harvested data is available under.

    Attached to every record's provenance so downstream users always know what
    they may do with it. ``commercial_use=None`` means "not established".
    """

    model_config = ConfigDict(frozen=True)

    name: str
    url: str | None = None
    attribution: str | None = None
    commercial_use: bool | None = None


class Request(BaseModel):
    """A URL to fetch and the parse callback that will handle it.

    Requests are serialised into the persistent frontier, so everything here
    must be JSON-compatible; ``callback`` is a method *name* on the source.
    """

    url: str
    callback: str = "parse"
    meta: dict[str, Any] = Field(default_factory=dict)
    priority: int = 0
    headers: dict[str, str] = Field(default_factory=dict)
    cache: CachePolicy = CachePolicy.PREFER
    dont_filter: bool = False

    @property
    def fingerprint(self) -> str:
        """Stable identity used for de-duplication and as the raw-store key."""
        return url_fingerprint(self.url)


class Record(BaseModel):
    """One unit of extracted data, e.g. an act, a judgment or a file reference."""

    kind: str
    id: str
    data: dict[str, Any] = Field(default_factory=dict)


class Provenance(BaseModel):
    """Where a record came from, written alongside it in the output."""

    source: str
    url: str
    fetched_at: datetime
    sha256: str
    status: int
    parser_version: str
    harvester_version: str
    license: DataLicense | None = None


def url_fingerprint(url: str) -> str:
    """Hash of the canonical URL (sorted query, no fragment)."""
    canonical = canonicalize_url(url, keep_fragments=False)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()
