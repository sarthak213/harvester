"""India Code — the Government of India's official repository of legislation.

India Code moved to https://indiacode.gov.in in 2026 and runs DSpace 9, so this
source is a thin layer over :class:`DSpaceSource` that knows the repository's
layout and turns its metadata into clean act records.

By default it harvests every central Act (≈1,750, including repealed ones) with
the text extraction DSpace provides; ``-o pdf=true`` also downloads the PDFs.
"""

from __future__ import annotations

from typing import Any

from harvester.models import DataLicense, Record
from harvester.sources.dspace import DSpaceSource, flatten_metadata

CENTRAL_COMMUNITY = "f467b316-98f0-4c08-a722-a2627e45bc19"


class IndiaCodeSource(DSpaceSource):
    name = "india-code"
    description = "Central Acts of India from India Code (indiacode.gov.in)"
    parser_version = "1"
    allowed_domains = ("indiacode.gov.in",)
    delay = 1.0
    concurrency = 1
    # RFC 9309 says an unreachable robots.txt means "disallow"; this is a
    # deliberate, documented exception (decided 2026-10-03).
    robots_unavailable = "allow"
    robots_unavailable_reason = (
        "indiacode.gov.in/robots.txt has returned HTTP 500 since the July 2026 site "
        "migration (checked 2026-10-03). The content is public Government of India "
        "legislation (Copyright Act s.52(1)(q)) served through the site's public DSpace "
        "REST API; harvested at one request per second with an identifying User-Agent."
    )
    license = DataLicense(
        name="Government of India legislation; reproduction permitted by "
        "Indian Copyright Act 1957 s.52(1)(q)",
        url="https://indiacode.gov.in",
        attribution="Source: India Code, Legislative Department, Ministry of Law and Justice",
        commercial_use=None,  # confirm with counsel before commercial redistribution
    )

    @classmethod
    def option_defaults(cls) -> dict[str, Any]:
        return {
            **super().option_defaults(),
            "base_url": "https://indiacode.gov.in/server",
            "scope": CENTRAL_COMMUNITY,
            "query": "dc.identifier.collection:ACT",
            "in_force_only": False,
            "pdf": False,
        }

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        if _truthy(self.options["in_force_only"]):
            self.options["query"] += " AND dc.identifier.repealed:false"
        self.options["bundles"] = "ORIGINAL,TEXT" if _truthy(self.options["pdf"]) else "TEXT"

    def item_record(self, item: dict[str, Any]) -> Record:
        md = flatten_metadata(item.get("metadata", {}))

        def first(key: str) -> str | None:
            values = md.get(key) or []
            return values[0] if values else None

        ref = first("dc.identifier.refact")
        return Record(
            kind="act",
            id=first("dc.identifier.act_id") or item["uuid"],
            data={
                "title": first("dc.title") or item.get("name"),
                "long_title": first("dc.title.long_title"),
                "act_number": first("dc.identifier.act_number"),
                "year": first("dc.date.act_year"),
                "enacted_on": first("dc.date.enact_date"),
                "in_force_from": first("dc.date.enforcement_date"),
                "repealed": first("dc.identifier.repealed") == "true",
                "jurisdiction": first("dc.identifier.state_name"),
                "ministry": first("dc.identifier.ministry_name"),
                "department": first("dc.identifier.department_name"),
                "last_modified": first("dc.date.last_modified"),
                "central_act_id": ref if ref and ref != "0" else None,
                "uuid": item["uuid"],
                "handle": item.get("handle"),
                "metadata": md,
            },
        )


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
