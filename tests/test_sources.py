"""Built-in sources: OAI-PMH against a live local endpoint, DSpace/India Code against fixtures."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from harvester import Page, Record, Request
from harvester.engine import Harvester, Settings
from harvester.sources.dspace import DSpaceSource, dominant_script
from harvester.sources.india_code import CENTRAL_COMMUNITY, IndiaCodeSource
from harvester.sources.oai_pmh import OAIPMHError, OAIPMHSource

from .conftest import FIXTURES, Hit, Site


def page_for(request: Request, body: bytes, content_type: str = "application/json") -> Page:
    return Page(
        url=request.url,
        request=request,
        status=200,
        headers={"content-type": content_type},
        body=body,
        sha256=hashlib.sha256(body).hexdigest(),
        fetched_at=datetime.now(timezone.utc),
    )


# ── OAI-PMH ───────────────────────────────────────────────────────────

OAI_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
    "<responseDate>2026-10-03T00:00:00Z</responseDate>"
)


def oai_record(identifier: str, title: str) -> str:
    return (
        f"<record><header><identifier>{identifier}</identifier>"
        "<datestamp>2026-01-01</datestamp><setSpec>col_1</setSpec></header>"
        '<metadata><oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:title>{title}</dc:title><dc:creator>A</dc:creator><dc:creator>B</dc:creator>"
        "</oai_dc:dc></metadata></record>"
    )


async def test_oai_pmh_follows_resumption_tokens(site: Site, settings: Settings) -> None:
    site.route("/robots.txt", "", status=404)

    @site.handler("/oai?verb=ListRecords&metadataPrefix=oai_dc&set=col_1")
    def first(hit: Hit) -> tuple[int, dict[str, str], bytes]:
        body = (
            OAI_HEAD + "<ListRecords>" + oai_record("oai:x:1", "One") + oai_record("oai:x:2", "Two")
            + '<resumptionToken completeListSize="3">tok-1</resumptionToken>'
            "</ListRecords></OAI-PMH>"
        )  # fmt: skip
        return 200, {"Content-Type": "text/xml"}, body.encode()

    @site.handler("/oai?verb=ListRecords&resumptionToken=tok-1")
    def second(hit: Hit) -> tuple[int, dict[str, str], bytes]:
        body = (
            OAI_HEAD + "<ListRecords>"
            '<record><header status="deleted"><identifier>oai:x:3</identifier>'
            "<datestamp>2026-01-02</datestamp></header></record>"
            "<resumptionToken/></ListRecords></OAI-PMH>"
        )
        return 200, {"Content-Type": "text/xml"}, body.encode()

    source = OAIPMHSource(base_url=site.url("/oai"), set="col_1")
    engine = Harvester(source, settings)
    report = await engine.run()
    engine.close()

    rows = [
        json.loads(line) for line in report.records_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [(r["kind"], r["id"]) for r in rows] == [
        ("record", "oai:x:1"),
        ("record", "oai:x:2"),
        ("deleted", "oai:x:3"),
    ]
    assert rows[0]["data"]["metadata"] == {"title": ["One"], "creator": ["A", "B"]}
    assert rows[0]["data"]["sets"] == ["col_1"]


def test_oai_pmh_empty_result_and_errors() -> None:
    source = OAIPMHSource(base_url="http://e.com/oai")
    request = next(iter(source.start()))
    empty = OAI_HEAD + '<error code="noRecordsMatch">none</error></OAI-PMH>'
    assert list(source.parse_list(page_for(request, empty.encode(), "text/xml"))) == []
    bad = OAI_HEAD + '<error code="badArgument">nope</error></OAI-PMH>'
    with pytest.raises(OAIPMHError, match="badArgument"):
        list(source.parse_list(page_for(request, bad.encode(), "text/xml")))


def test_oai_pmh_requires_base_url() -> None:
    with pytest.raises(ValueError, match="base_url"):
        list(OAIPMHSource().start())


def test_unknown_option_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown option"):
        OAIPMHSource(base_url="x", colour="blue")


# ── DSpace / India Code ───────────────────────────────────────────────


def test_india_code_start_targets_central_acts() -> None:
    request = next(iter(IndiaCodeSource().start()))
    assert request.url.startswith("https://indiacode.gov.in/server/api/discover/search/objects?")
    assert f"scope={CENTRAL_COMMUNITY}" in request.url
    assert "dc.identifier.collection%3AACT" in request.url
    in_force = next(iter(IndiaCodeSource(in_force_only="true").start()))
    assert "repealed%3Afalse" in in_force.url


def test_india_code_maps_acts_and_queues_files() -> None:
    source = IndiaCodeSource()
    request = next(iter(source.start()))
    body = (FIXTURES / "india_code" / "search_page.json").read_bytes()
    out = list(source.parse_search(page_for(request, body)))

    acts = [o for o in out if isinstance(o, Record)]
    follow = [o for o in out if isinstance(o, Request)]
    assert [a.data["title"] for a in acts] == [
        "The National Medical Commission Act, 2019",
        "The Cost Accountants Act, 1959",
        "The Delhi Special Police Establishment Act, 1946",
    ]
    nmc = acts[0]
    assert nmc.kind == "act" and nmc.id.startswith("AC_CEN_")
    assert nmc.data["jurisdiction"] == "CENTRAL" and nmc.data["year"] == "2019"
    assert isinstance(nmc.data["repealed"], bool)

    bundle_requests = [r for r in follow if r.callback == "parse_bundles"]
    assert len(bundle_requests) == 3
    assert any(r.callback == "parse_search" for r in follow), "next page not followed"


def test_india_code_downloads_text_only_by_default() -> None:
    body = (FIXTURES / "india_code" / "bundles_bns.json").read_bytes()
    request = Request(
        url="https://indiacode.gov.in/x", callback="parse_bundles", meta={"item": "i"}
    )

    text_only = list(IndiaCodeSource().parse_bundles(page_for(request, body)))
    assert [r.meta["name"] for r in text_only if isinstance(r, Request)] == [
        "a2023-45.pdf.txt",
        "Hh202345.pdf.txt",
    ]
    with_pdf = list(
        IndiaCodeSource(pdf="true", max_file_mb=10).parse_bundles(page_for(request, body))
    )
    names = {r.meta["name"] for r in with_pdf if isinstance(r, Request)}
    assert "a2023-45.pdf" in names
    too_big = [r for r in with_pdf if isinstance(r, Record)]
    assert [r.data["name"] for r in too_big] == ["Hh202345.pdf"]  # 40 MB > 10 MB limit


def test_dspace_file_is_verified_and_text_inlined() -> None:
    text = (FIXTURES / "india_code" / "bns_text_head.txt").read_bytes()
    meta = {
        "item": "i",
        "bitstream": "b1",
        "name": "a.pdf.txt",
        "md5": hashlib.md5(text).hexdigest(),
    }
    request = Request(url="https://e.com/content", callback="parse_file", meta=meta)
    (record,) = DSpaceSource(base_url="https://e.com/server").parse_file(
        page_for(request, text, "text/plain;charset=UTF-8")
    )
    assert record.kind == "file" and record.id == "b1"
    assert record.data["md5_verified"] is True
    assert "BHARATIYA NYAYA SANHITA" in record.data["text"]
    assert record.data["script"] == "latin"

    meta["md5"] = "0" * 32
    (bad,) = DSpaceSource(base_url="https://e.com/server").parse_file(
        page_for(request.model_copy(update={"meta": meta}), text, "text/plain")
    )
    assert bad.data["md5_verified"] is False


def test_dominant_script() -> None:
    assert dominant_script("Punishment for murder") == "latin"
    assert dominant_script("हत्या के लिए दंड") == "devanagari"
    assert dominant_script("123 ...") is None
