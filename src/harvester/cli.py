"""Command-line interface."""

from __future__ import annotations

import asyncio
import json
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import FrameType
from typing import Annotated, Any, Optional

import typer
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from harvester import __version__
from harvester.engine import Harvester, RunReport, Settings
from harvester.fetch import DEFAULT_USER_AGENT, ScraplingFetcher
from harvester.frontier import Frontier
from harvester.models import url_fingerprint
from harvester.politeness import RobotsPolicy
from harvester.registry import SourceNotFound, installed_sources, load_source
from harvester.source import Source
from harvester.store import RawStore

app = typer.Typer(
    name="harvester",
    help="Polite, reproducible web harvesting: fetch once, parse forever.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    pretty_exceptions_show_locals=False,
)
console = Console()

DataDir = Annotated[
    Path, typer.Option("--data-dir", "-d", help="Where stores, queues and runs live.")
]
Options = Annotated[
    Optional[list[str]],  # noqa: UP045 — typer needs Optional on 3.10
    typer.Option("--option", "-o", help="Source option as key=value (repeatable)."),
]


def _version(value: bool) -> None:
    if value:
        console.print(f"harvester {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="Show version.")
    ] = False,
) -> None:
    """Polite, reproducible web harvesting: fetch once, parse forever."""


# ── Helpers ─────────────────────────────────────────────────────────────


def _make_source(spec: str, options: list[str] | None) -> Source:
    try:
        cls = load_source(spec)
    except SourceNotFound as exc:
        console.print(f"[red]error:[/] {exc}")
        raise typer.Exit(2) from None
    parsed: dict[str, Any] = {}
    for item in options or []:
        key, sep, value = item.partition("=")
        if not sep:
            console.print(f"[red]error:[/] option {item!r} is not key=value")
            raise typer.Exit(2)
        parsed[key.strip()] = value
    try:
        return cls(**parsed)
    except ValueError as exc:
        console.print(f"[red]error:[/] {exc}")
        raise typer.Exit(2) from None


def _human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _dashboard(engine: Harvester, started: float) -> Panel:
    s = engine.stats
    frontier = engine.frontier.counts() if engine.frontier else {}
    elapsed = max(time.monotonic() - started, 1e-6)

    table = Table.grid(padding=(0, 3))
    for _ in range(4):
        table.add_column(justify="right", style="bold")
        table.add_column(style="dim")
    table.add_row(
        str(s.pages), "pages", str(s.records), "records",
        str(s.fetched), "fetched", str(s.from_cache + s.not_modified), "from cache",
    )  # fmt: skip
    table.add_row(
        str(frontier.get("pending", 0)), "queued", str(s.retries), "retries",
        str(s.failed), "failed", str(s.robots_blocked), "blocked",
    )  # fmt: skip
    table.add_row(
        _human_bytes(s.bytes), "downloaded", f"{s.pages / elapsed * 60:.0f}", "pages/min",
        f"{elapsed:.0f}s", "elapsed", str(s.offsite + s.missing), "skipped",
    )  # fmt: skip

    parts: list[Any] = [table]
    if s.recent_errors:
        errors = Text("\n".join(list(s.recent_errors)[-4:]), style="yellow", overflow="ellipsis")
        parts += [Text(""), errors]
    title = f"[bold]{engine.source.name}[/] | {engine._mode}"
    return Panel(Group(*parts), title=title, border_style="cyan", expand=False)


def _run_with_dashboard(
    engine: Harvester, coro_factory: Callable[[], Awaitable[RunReport]]
) -> RunReport:
    """Run the engine with a live view; first Ctrl+C stops gracefully, second aborts."""
    interrupts = 0
    loop: asyncio.AbstractEventLoop | None = None

    def on_sigint(signum: int, frame: FrameType | None) -> None:
        nonlocal interrupts
        interrupts += 1
        if interrupts == 1 and loop is not None:
            console.print("[yellow]stopping after in-flight requests... (Ctrl+C again to abort)[/]")
            loop.call_soon_threadsafe(engine.stop)
        else:
            raise KeyboardInterrupt

    async def runner() -> RunReport:
        nonlocal loop
        loop = asyncio.get_running_loop()
        started = time.monotonic()
        with Live(_dashboard(engine, started), console=console, refresh_per_second=4) as live:
            task: asyncio.Future[RunReport] = asyncio.ensure_future(coro_factory())
            while not task.done():
                live.update(_dashboard(engine, started))
                await asyncio.sleep(0.25)
            live.update(_dashboard(engine, started))
            return await task

    previous = signal.signal(signal.SIGINT, on_sigint)
    try:
        return asyncio.run(runner())
    finally:
        signal.signal(signal.SIGINT, previous)


def _print_report(report: RunReport) -> None:
    colour = {"complete": "green", "limit": "cyan", "interrupted": "yellow"}[report.outcome]
    console.print(f"[{colour}]{report.outcome}[/] run [bold]{report.run_id}[/]")
    kinds = ", ".join(f"{n} {k}" for k, n in sorted(report.records_by_kind.items())) or "none"
    console.print(f"  records: {kinds}")
    console.print(f"  output:  {report.records_path}")
    if report.frontier.get("failed") or report.frontier.get("skipped"):
        console.print(
            f"  [yellow]{report.frontier.get('failed', 0)} failed, "
            f"{report.frontier.get('skipped', 0)} skipped[/] - see `harvester status`"
        )


# ── Commands ────────────────────────────────────────────────────────────


@app.command("list")
def list_sources() -> None:
    """Show installed sources."""
    table = Table(title="Installed sources", title_justify="left")
    table.add_column("name", style="bold cyan")
    table.add_column("description")
    table.add_column("data licence", style="dim")
    for name, cls in installed_sources().items():
        table.add_row(name, cls.description, cls.license.name if cls.license else "-")
    console.print(table)


@app.command()
def run(
    source: Annotated[str, typer.Argument(help="Source name, or path/to/source.py[:Class].")],
    option: Options = None,
    data_dir: DataDir = Path(".harvester"),
    limit: Annotated[Optional[int], typer.Option(help="Stop after this many pages.")] = None,  # noqa: UP045
    concurrency: Annotated[int, typer.Option(help="Concurrent workers in total.")] = 4,
    delay: Annotated[float, typer.Option(help="Seconds between requests to one host.")] = 1.0,
    refresh: Annotated[
        bool, typer.Option(help="Revalidate cached responses (conditional requests).")
    ] = False,
    restart: Annotated[
        bool, typer.Option(help="Discard unfinished progress and start a new pass.")
    ] = False,
    user_agent: Annotated[str, typer.Option(help="User-Agent; keep a contact URL in it.")] = (
        DEFAULT_USER_AGENT
    ),
    robots_unavailable: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option(
            help="Override the source's policy when robots.txt is unreachable: allow|disallow."
        ),
    ] = None,
) -> None:
    """Crawl a source. Resumes automatically if the last run was interrupted."""
    src = _make_source(source, option)
    if robots_unavailable not in (None, "allow", "disallow"):
        console.print("[red]error:[/] --robots-unavailable must be allow or disallow")
        raise typer.Exit(2)
    settings = Settings(
        data_dir=data_dir,
        concurrency=concurrency,
        delay=delay,
        limit=limit,
        refresh=refresh,
        restart=restart,
        user_agent=user_agent,
        robots_unavailable=robots_unavailable,
    )
    engine = Harvester(src, settings)
    try:
        report = _run_with_dashboard(engine, engine.run)
    finally:
        engine.close()
    _print_report(report)


@app.command()
def reparse(
    source: Annotated[str, typer.Argument(help="Source name, or path/to/source.py[:Class].")],
    option: Options = None,
    data_dir: DataDir = Path(".harvester"),
) -> None:
    """Re-run parsers over the raw store. Makes no network requests."""
    src = _make_source(source, option)
    engine = Harvester(src, Settings(data_dir=data_dir, concurrency=1))
    try:
        report = _run_with_dashboard(engine, engine.reparse)
    finally:
        engine.close()
    _print_report(report)
    if report.stats.get("missing"):
        console.print(
            f"  [yellow]{report.stats['missing']} requests were never fetched; "
            "run the crawl to fill them in[/]"
        )


@app.command()
def status(
    source: Annotated[str, typer.Argument(help="Source name, or path/to/source.py[:Class].")],
    data_dir: DataDir = Path(".harvester"),
) -> None:
    """Show queue, store and recent runs for a source."""
    cls = load_source(source)
    root = data_dir / cls.name
    if not root.exists():
        console.print(f"no data for {cls.name} in {data_dir}")
        raise typer.Exit(1)

    store = RawStore(root / "store")
    stats = store.stats()
    store.close()
    console.print(
        f"[bold]{cls.name}[/] raw store: {stats['responses']} responses, "
        f"{stats['unique_bodies']} unique bodies, {_human_bytes(stats['bytes'])}"
    )

    frontier_path = root / "frontier.sqlite"
    if frontier_path.exists():
        frontier = Frontier(frontier_path)
        counts = frontier.counts()
        console.print("queue: " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())))
        problems = frontier.problems()
        frontier.close()
        if problems:
            table = Table(title="Failed / skipped", title_justify="left")
            table.add_column("state")
            table.add_column("url", overflow="fold")
            table.add_column("reason", style="yellow", overflow="fold")
            for state, url, reason in problems:
                table.add_row(state, url, reason)
            console.print(table)

    runs = sorted((root / "runs").glob("*/manifest.json"), reverse=True)[:5]
    if runs:
        table = Table(title="Recent runs", title_justify="left")
        for col in ("run", "outcome", "pages", "records", "fetched", "duration"):
            table.add_column(col)
        for path in runs:
            m = json.loads(path.read_text(encoding="utf-8"))
            table.add_row(
                m["run_id"],
                m["outcome"],
                str(m["stats"]["pages"]),
                str(m["stats"]["records"]),
                str(m["stats"]["fetched"]),
                f"{m['duration_s']:.0f}s",
            )
        console.print(table)


@app.command()
def changes(
    source: Annotated[str, typer.Argument(help="Source name, or path/to/source.py[:Class].")],
    data_dir: DataDir = Path(".harvester"),
) -> None:
    """List URLs whose content changed between fetches."""
    cls = load_source(source)
    store = RawStore(data_dir / cls.name / "store")
    rows = store.changed()
    store.close()
    if not rows:
        console.print("no content changes recorded")
        return
    for url, versions in rows:
        console.print(f"{versions:>3} versions  {url}")


@app.command()
def show(
    url: Annotated[str, typer.Argument(help="A URL that was fetched.")],
    source: Annotated[str, typer.Option("--source", "-s", help="Source it was fetched by.")],
    data_dir: DataDir = Path(".harvester"),
    save: Annotated[
        Optional[Path],  # noqa: UP045
        typer.Option(help="Write the stored body to this file."),
    ] = None,
) -> None:
    """Inspect a stored response (metadata, and optionally the body)."""
    cls = load_source(source)
    store = RawStore(data_dir / cls.name / "store")
    snapshot = store.get(url_fingerprint(url))
    if snapshot is None:
        store.close()
        console.print("[red]not in the raw store[/]")
        raise typer.Exit(1)
    console.print_json(
        json.dumps(
            {
                "url": snapshot.url,
                "final_url": snapshot.final_url,
                "status": snapshot.status,
                "sha256": snapshot.sha256,
                "size": snapshot.size,
                "fetched_at": snapshot.fetched_at.isoformat(),
                "validated_at": snapshot.validated_at.isoformat(),
                "headers": snapshot.headers,
            }
        )
    )
    if save:
        save.write_bytes(store.read_blob(snapshot.sha256))
        console.print(f"saved {snapshot.size} bytes to {save}")
    store.close()


@app.command()
def robots(
    url: Annotated[str, typer.Argument(help="URL to check.")],
    user_agent: Annotated[str, typer.Option(help="User-Agent to evaluate.")] = DEFAULT_USER_AGENT,
) -> None:
    """Explain whether harvester may fetch a URL, per robots.txt (RFC 9309)."""

    async def check() -> None:
        fetcher = ScraplingFetcher(user_agent)
        policy = RobotsPolicy(lambda u: fetcher.fetch(u, {}), user_agent)
        try:
            decision = await policy.check(url)
        finally:
            await fetcher.close()
        verdict = "[green]allowed[/]" if decision.allowed else "[red]disallowed[/]"
        console.print(f"{verdict}  {decision.reason}")
        if decision.crawl_delay:
            console.print(f"crawl-delay: {decision.crawl_delay}s")

    asyncio.run(check())


_TEMPLATE = '''"""{title} — a harvester source."""

from harvester import CachePolicy, DataLicense, Page, Request, Source


class {cls}(Source):
    name = "{name}"
    description = "TODO: one line about what this harvests"
    start_urls = ("https://example.org/",)
    allowed_domains = ("example.org",)
    license = DataLicense(name="TODO: the data's licence", commercial_use=None)

    def start(self):
        # Listings change, so revalidate them; detail pages are cached forever.
        for url in self.start_urls:
            yield Request(url=url, cache=CachePolicy.REVALIDATE)

    def parse(self, page: Page):
        for link in page.css("a.item::attr(href)").getall():
            yield page.follow(link, callback="parse_item")
        if next_page := page.css("a.next::attr(href)").get():
            yield page.follow(next_page, cache=CachePolicy.REVALIDATE)

    def parse_item(self, page: Page):
        yield page.record(
            "item",
            id=page.url,
            title=page.css("h1::text").get(),
        )
'''


@app.command()
def new(
    name: Annotated[str, typer.Argument(help="Source name, e.g. my-site.")],
    directory: Annotated[Path, typer.Option(help="Where to write the file.")] = Path(),
) -> None:
    """Scaffold a new source file."""
    stem = name.replace("-", "_")
    cls = "".join(part.capitalize() for part in stem.split("_")) + "Source"
    path = directory / f"{stem}.py"
    if path.exists():
        console.print(f"[red]error:[/] {path} already exists")
        raise typer.Exit(1)
    path.write_text(_TEMPLATE.format(title=name, cls=cls, name=name), encoding="utf-8")
    console.print(f"created {path}\nrun it with: [bold]harvester run {path} --limit 5[/]")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
