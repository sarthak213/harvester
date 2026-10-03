"""The CLI, driven through Typer's test runner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harvester.cli import app
from harvester.registry import SourceNotFound, load_source

from .conftest import Site

runner = CliRunner()


def test_list_shows_builtin_sources() -> None:
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    for name in ("oai-pmh", "dspace", "india-code"):
        assert name in result.output


def test_new_scaffolds_a_loadable_source(tmp_path: Path) -> None:
    result = runner.invoke(app, ["new", "my-site", "--directory", str(tmp_path)])
    assert result.exit_code == 0, result.output
    cls = load_source(str(tmp_path / "my_site.py"))
    assert cls.name == "my-site" and cls.__name__ == "MySiteSource"
    again = runner.invoke(app, ["new", "my-site", "--directory", str(tmp_path)])
    assert again.exit_code == 1


def test_load_source_errors(tmp_path: Path) -> None:
    with pytest.raises(SourceNotFound, match="unknown source"):
        load_source("does-not-exist")
    with pytest.raises(SourceNotFound, match="no such file"):
        load_source(str(tmp_path / "missing.py"))


def test_run_reparse_status_end_to_end(site: Site, tmp_path: Path) -> None:
    site.route("/robots.txt", "", status=404)
    site.route("/", '<a href="/a">a</a><a href="/b">b</a>')
    site.route("/a", "<h1>Alpha</h1>")
    site.route("/b", "<h1>Beta</h1>")
    source_file = tmp_path / "tiny.py"
    source_file.write_text(
        f'''
from harvester import Source

class Tiny(Source):
    name = "tiny"
    start_urls = ("{site.url("/")}",)

    def parse(self, page):
        for href in page.css("a::attr(href)").getall():
            yield page.follow(href, callback="item")

    def item(self, page):
        yield page.record("page", page.url, title=page.css("h1::text").get())
''',
        encoding="utf-8",
    )
    data = tmp_path / "data"

    result = runner.invoke(app, ["run", str(source_file), "-d", str(data), "--delay", "0"])
    assert result.exit_code == 0, result.output
    assert "complete" in result.output

    result = runner.invoke(app, ["reparse", str(source_file), "-d", str(data)])
    assert result.exit_code == 0, result.output

    manifests = sorted((data / "tiny" / "runs").glob("*/manifest.json"))
    assert len(manifests) == 2
    modes = [json.loads(m.read_text(encoding="utf-8"))["mode"] for m in manifests]
    assert sorted(modes) == ["crawl", "reparse"]

    result = runner.invoke(app, ["status", str(source_file), "-d", str(data)])
    assert result.exit_code == 0, result.output
    assert "3 responses" in result.output

    result = runner.invoke(app, ["show", site.url("/a"), "-s", str(source_file), "-d", str(data)])
    assert result.exit_code == 0, result.output
    assert '"status": 200' in result.output


def test_bad_option_syntax_is_reported(tmp_path: Path) -> None:
    result = runner.invoke(app, ["run", "oai-pmh", "-o", "base_url", "-d", str(tmp_path)])
    assert result.exit_code == 2
    assert "key=value" in result.output
