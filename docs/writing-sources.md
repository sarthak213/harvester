# Writing sources

A source tells harvester where to start and how to read what comes back. Everything else (queueing, caching, politeness, retries, output) is the engine's job.

## Anatomy

```python
from harvester import CachePolicy, DataLicense, Page, Request, Source


class Gazette(Source):
    name = "gazette"  # CLI name and data-directory name
    description = "Notifications from the Example Gazette"
    allowed_domains = ("gazette.example.org",)  # anything else is skipped
    license = DataLicense(
        name="Government open data licence",
        url="https://gazette.example.org/terms",
        attribution="Source: Example Gazette",
        commercial_use=True,
    )
    parser_version = "2"  # bump when output changes
    delay = 2.0  # seconds between requests to this host
    concurrency = 1  # simultaneous requests to this host

    @classmethod
    def option_defaults(cls):
        return {"year": "2026"}  # harvester run gazette -o year=2025

    def start(self):
        url = f"https://gazette.example.org/{self.options['year']}/"
        yield Request(url=url, callback="parse_index", cache=CachePolicy.REVALIDATE)

    def parse_index(self, page: Page):
        for href in page.css("a.notice::attr(href)").getall():
            yield page.follow(href, callback="parse_notice", meta={"year": self.options["year"]})
        if nxt := page.css("a[rel=next]::attr(href)").get():
            yield page.follow(nxt, cache=CachePolicy.REVALIDATE)

    def parse_notice(self, page: Page):
        yield page.record(
            "notice",
            id=page.css("meta[name=notice-id]::attr(content)").get(),
            title=page.css("h1::text").get(),
            year=page.meta["year"],
        )
        if pdf := page.css("a.pdf::attr(href)").get():
            yield page.follow(pdf, callback="parse_pdf")

    def parse_pdf(self, page: Page):
        # The PDF body is already in the raw store; record where to find it.
        yield page.record("file", id=page.sha256, url=page.url, bytes=len(page.body))
```

## Callbacks

- A callback is any method named in `Request.callback` (default `parse`).
- It receives a `Page` and yields `Request` and `Record` objects. Use `page.follow()` and `page.record()` for convenience.
- **Callbacks must not perform I/O.** Everything they need is in the page: `body`, `headers`, `text`, `json()`, `css()`, `xpath()` and `meta`. Purity is what lets `harvester reparse` reproduce a crawl exactly from the raw store.
- An exception inside a callback fails only that request. The reason is visible in `harvester status`, and the crawl continues.

## Cache policies

| Policy | Use for | Behaviour on later passes |
|---|---|---|
| `PREFER` (default) | documents, files, anything immutable | served from the raw store; no request |
| `REVALIDATE` | listings, feeds, search results | conditional request; `304` reuses the stored body |
| `BYPASS` | volatile endpoints | always re-downloaded |

`harvester run --refresh` upgrades every `PREFER` request to `REVALIDATE` for one pass.

## Passing data between callbacks

Put it in `meta`. It is stored with the request in the frontier, so it survives restarts. `page.follow()` merges the current page's meta with any you add.

## robots.txt that cannot be fetched

If a site's robots.txt returns `5xx` or times out, RFC 9309 says crawlers must assume everything is disallowed, and harvester does. If you have good reason to proceed (for example, the operator has confirmed it, or the endpoint is a published bulk API), declare it:

```python
robots_unavailable = "allow"
robots_unavailable_reason = "Operator confirmed by email on 2026-10-01; bulk API is public."
```

The reason is written into every run manifest.

## Packaging a source as a plugin

Any installed package can register sources:

```toml
# pyproject.toml of your package
[project.entry-points."harvester.sources"]
gazette = "my_package.sources:Gazette"
```

After `pip install`, `harvester list` shows it and `harvester run gazette` works.
