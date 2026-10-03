"""Generic DSpace 7+ REST API harvester.

DSpace is the most widely used repository platform (universities, archives,
government portals). From version 7 it has a HAL/JSON REST API; this source
pages through a discovery search, emits one record per item, and downloads the
item's files from the bundles you choose, verifying each against its MD5.

    harvester run dspace -o base_url=https://repo.example.org/server \\
                         -o scope=<community-or-collection-uuid> -o bundles=ORIGINAL
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode

from harvester.models import CachePolicy, Record, Request
from harvester.page import Page
from harvester.source import Source


class DSpaceSource(Source):
    name = "dspace"
    description = "Any DSpace 7+ repository via its REST API (items + verified files)"
    parser_version = "1"

    @classmethod
    def option_defaults(cls) -> dict[str, Any]:
        return {
            "base_url": None,  # the REST server root, e.g. https://host/server
            "scope": None,  # community or collection UUID
            "query": "*",
            "page_size": 100,
            "bundles": "ORIGINAL",  # comma-separated; empty for metadata only
            "max_file_mb": 50,
            "max_text_chars": 2_000_000,  # inline text/* files up to this size
        }

    # ── Discovery ───────────────────────────────────────────────────────

    def start(self) -> Iterator[Request]:
        if not self.options["base_url"]:
            raise ValueError(f"{self.name} needs -o base_url=<DSpace REST root>")
        params: dict[str, Any] = {
            "query": self.options["query"],
            "dsoType": "ITEM",
            "size": int(self.options["page_size"]),
            "page": 0,
            "sort": "dc.date.accessioned,ASC",
        }
        if self.options["scope"]:
            params["scope"] = self.options["scope"]
        url = f"{self.api}/discover/search/objects?{urlencode(params)}"
        yield Request(url=url, callback="parse_search", cache=CachePolicy.REVALIDATE)

    @property
    def api(self) -> str:
        return f"{str(self.options['base_url']).rstrip('/')}/api"

    def parse_search(self, page: Page) -> Iterator[Request | Record]:
        result = page.json()["_embedded"]["searchResult"]
        for obj in result.get("_embedded", {}).get("objects", []):
            item = obj["_embedded"]["indexableObject"]
            yield self.item_record(item)
            if self._wanted_bundles():
                yield Request(
                    url=f"{self.api}/core/items/{item['uuid']}/bundles"
                    "?embed=bitstreams&embed.size=bitstreams=100&size=50",
                    callback="parse_bundles",
                    meta={"item": item["uuid"]},
                )
        next_link = result.get("_links", {}).get("next", {}).get("href")
        if next_link:
            yield Request(url=next_link, callback="parse_search", cache=CachePolicy.REVALIDATE)

    def item_record(self, item: dict[str, Any]) -> Record:
        """Override to map repository-specific metadata into clean fields."""
        return Record(
            kind="item",
            id=item["uuid"],
            data={
                "name": item.get("name"),
                "handle": item.get("handle"),
                "last_modified": item.get("lastModified"),
                "metadata": flatten_metadata(item.get("metadata", {})),
            },
        )

    # ── Files ───────────────────────────────────────────────────────────

    def _wanted_bundles(self) -> set[str]:
        return {b.strip() for b in str(self.options["bundles"] or "").split(",") if b.strip()}

    def parse_bundles(self, page: Page) -> Iterator[Request | Record]:
        wanted = self._wanted_bundles()
        max_bytes = float(self.options["max_file_mb"]) * 1024 * 1024
        for bundle in page.json().get("_embedded", {}).get("bundles", []):
            if bundle["name"] not in wanted:
                continue
            bitstreams = bundle["_embedded"]["bitstreams"]["_embedded"]["bitstreams"]
            for bs in bitstreams:
                info = {
                    "item": page.meta["item"],
                    "bundle": bundle["name"],
                    "bitstream": bs["uuid"],
                    "name": bs.get("name"),
                    "size": bs.get("sizeBytes"),
                    "md5": (bs.get("checkSum") or {}).get("value"),
                }
                if info["size"] and info["size"] > max_bytes:
                    yield Record(kind="file", id=bs["uuid"], data={**info, "skipped": "too large"})
                    continue
                yield Request(
                    url=bs["_links"]["content"]["href"],
                    callback="parse_file",
                    meta=info,
                )

    def parse_file(self, page: Page) -> Iterator[Record]:
        meta = dict(page.meta)
        md5 = hashlib.md5(page.body, usedforsecurity=False).hexdigest()
        data: dict[str, Any] = {
            **meta,
            "content_type": page.content_type,
            "bytes": len(page.body),
            "sha256": page.sha256,  # the blob's key in the raw store
            "md5_verified": md5 == meta.get("md5") if meta.get("md5") else None,
        }
        if page.content_type.startswith("text/") and len(page.body) <= int(
            self.options["max_text_chars"]
        ):
            text = page.text
            data["text"] = text
            data["script"] = dominant_script(text)
        yield Record(kind="file", id=meta["bitstream"], data=data)


def flatten_metadata(metadata: dict[str, list[dict[str, Any]]]) -> dict[str, list[str]]:
    """DSpace returns ``{"dc.title": [{"value": "...", "language": ...}]}``; keep the values."""
    return {key: [v["value"] for v in values if v.get("value")] for key, values in metadata.items()}


def dominant_script(text: str, sample: int = 20_000) -> str | None:
    """Rough script detection, e.g. to tell English from Hindi text extractions."""
    counts: dict[str, int] = {}
    for ch in text[:sample]:
        if ch.isalpha():
            script = unicodedata.name(ch, "UNKNOWN").split(" ")[0]
            counts[script] = counts.get(script, 0) + 1
    return max(counts, key=lambda k: counts[k]).lower() if counts else None
