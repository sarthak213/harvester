"""quotes.toscrape.com — a sandbox site built for practising scraping.

harvester run examples/quotes.py
"""

from collections.abc import Iterator

from harvester import CachePolicy, DataLicense, Page, Record, Request, Source


class QuotesSource(Source):
    name = "quotes"
    description = "Quotes and author bios from quotes.toscrape.com (scraping sandbox)"
    start_urls = ("https://quotes.toscrape.com/",)
    allowed_domains = ("quotes.toscrape.com",)
    license = DataLicense(name="Scraping sandbox by Zyte; sample data", commercial_use=None)

    def start(self) -> Iterator[Request]:
        for url in self.start_urls:
            yield Request(url=url, cache=CachePolicy.REVALIDATE)

    def parse(self, page: Page) -> Iterator[Request | Record]:
        for quote in page.css("div.quote"):
            author_href = quote.css("a[href^='/author/']::attr(href)").get()
            yield page.record(
                "quote",
                id=f"{page.url}#{quote.css('span.text::text').get()[:40]}",
                text=quote.css("span.text::text").get(),
                author=quote.css("small.author::text").get(),
                tags=quote.css("a.tag::text").getall(),
            )
            if author_href:
                yield page.follow(author_href, callback="parse_author")

        if next_href := page.css("li.next a::attr(href)").get():
            yield page.follow(next_href, cache=CachePolicy.REVALIDATE)

    def parse_author(self, page: Page) -> Iterator[Record]:
        yield page.record(
            "author",
            id=page.url.rstrip("/").rsplit("/", 1)[-1],
            name=page.css("h3.author-title::text").get("").strip(),
            born=page.css("span.author-born-date::text").get(),
            born_in=page.css("span.author-born-location::text").get(),
        )
