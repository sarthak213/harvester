"""Generic OAI-PMH 2.0 harvester.

OAI-PMH is the standard bulk-harvesting protocol of digital libraries and
institutional repositories (DSpace, EPrints, Fedora, Invenio, OJS …). It is
designed for exactly this use, so it is the first thing to look for on any
repository before scraping its HTML.

    harvester run oai-pmh -o base_url=https://example.org/oai/request -o set=col_123
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode

from lxml import etree

from harvester.models import CachePolicy, Record, Request
from harvester.page import Page
from harvester.source import Source

_DC = "{http://purl.org/dc/elements/1.1/}"


class OAIPMHError(RuntimeError):
    pass


class OAIPMHSource(Source):
    name = "oai-pmh"
    description = "Any OAI-PMH 2.0 repository (ListRecords with resumption tokens)"
    parser_version = "1"

    @classmethod
    def option_defaults(cls) -> dict[str, Any]:
        return {
            "base_url": None,
            "metadata_prefix": "oai_dc",
            "set": None,
            "from": None,
            "until": None,
        }

    def start(self) -> Iterator[Request]:
        if not self.options["base_url"]:
            raise ValueError("oai-pmh needs -o base_url=<OAI endpoint>")
        params = {"verb": "ListRecords", "metadataPrefix": self.options["metadata_prefix"]}
        for key in ("set", "from", "until"):
            if self.options[key]:
                params[key] = self.options[key]
        yield self._request(params)

    def _request(self, params: dict[str, str]) -> Request:
        return Request(
            url=f"{self.options['base_url']}?{urlencode(params)}",
            callback="parse_list",
            cache=CachePolicy.REVALIDATE,
        )

    def parse_list(self, page: Page) -> Iterator[Request | Record]:
        root = etree.fromstring(page.body)
        error = root.find("{*}error")
        if error is not None:
            code = error.get("code", "")
            if code == "noRecordsMatch":
                return  # a valid, empty result
            raise OAIPMHError(f"{code}: {(error.text or '').strip()}")

        for record in root.iterfind("{*}ListRecords/{*}record"):
            yield self._record(record)

        token = root.find("{*}ListRecords/{*}resumptionToken")
        if token is not None and (token.text or "").strip():
            yield self._request({"verb": "ListRecords", "resumptionToken": token.text.strip()})

    def _record(self, element: etree._Element) -> Record:
        header = element.find("{*}header")
        assert header is not None
        identifier = header.findtext("{*}identifier", default="").strip()
        data: dict[str, Any] = {
            "datestamp": header.findtext("{*}datestamp", default="").strip(),
            "sets": [s.text for s in header.iterfind("{*}setSpec") if s.text],
            "metadata_prefix": self.options["metadata_prefix"],
        }
        if header.get("status") == "deleted":
            return Record(kind="deleted", id=identifier, data=data)

        metadata = element.find("{*}metadata")
        payload = metadata[0] if metadata is not None and len(metadata) else None
        if payload is not None and self.options["metadata_prefix"] == "oai_dc":
            fields: dict[str, list[str]] = {}
            for child in payload:
                if isinstance(child.tag, str) and child.tag.startswith(_DC) and child.text:
                    fields.setdefault(child.tag[len(_DC) :], []).append(child.text.strip())
            data["metadata"] = fields
        elif payload is not None:
            data["metadata_xml"] = etree.tostring(payload, encoding="unicode")
        return Record(kind="record", id=identifier, data=data)
