"""Network layer. The default fetcher is Scrapling, configured to be honest.

Scrapling can impersonate browsers and forge headers; harvester deliberately
turns all of that off. Requests carry a real User-Agent that names the tool and
a contact URL, so site operators can see who is crawling and reach out.
"""

from __future__ import annotations

import logging
import time
import warnings
from dataclasses import dataclass
from typing import Any, Protocol

from harvester import __version__

DEFAULT_USER_AGENT = f"harvester/{__version__} (+https://github.com/sarthak213/harvester)"


class FetchError(Exception):
    """A network-level failure: DNS, connection, TLS, timeout."""


@dataclass(frozen=True)
class FetchResponse:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    elapsed: float


class Fetcher(Protocol):
    async def fetch(self, url: str, headers: dict[str, str]) -> FetchResponse: ...

    async def close(self) -> None: ...


class ScraplingFetcher:
    """Plain HTTP via Scrapling's curl_cffi session, with stealth disabled."""

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 60.0,
        max_body_bytes: int = 100 * 1024 * 1024,
    ) -> None:
        # On Windows curl_cffi warns that it adds a selector thread to the
        # Proactor loop. That is expected and works; don't alarm users.
        from curl_cffi.utils import CurlCffiWarning

        warnings.filterwarnings("ignore", category=CurlCffiWarning)
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_body_bytes = max_body_bytes
        # Scrapling's session classes are only partially typed.
        self._session: Any = None
        self._client: Any = None

    async def _ensure_session(self) -> Any:
        if self._client is None:
            from scrapling.fetchers import FetcherSession

            # Scrapling sets its logger to INFO on import and logs every fetch;
            # harvester has its own reporting, so quieten it afterwards.
            logging.getLogger("scrapling").setLevel(logging.WARNING)
            self._session = FetcherSession(
                impersonate=None,
                stealthy_headers=False,
                retries=1,  # retries and backoff are the engine's job
                timeout=self.timeout,
                follow_redirects=True,
                max_redirects=10,
            )
            self._client = await self._session.__aenter__()
        return self._client

    async def fetch(self, url: str, headers: dict[str, str]) -> FetchResponse:
        client = await self._ensure_session()
        # Per-request headers replace session headers in Scrapling, so always
        # send the full set.
        all_headers = {"User-Agent": self.user_agent, "Accept": "*/*", **headers}
        started = time.perf_counter()
        try:
            response = await client.get(url, headers=all_headers)
        except Exception as exc:
            raise FetchError(f"{type(exc).__name__}: {exc}") from exc
        body: bytes = response.body
        if len(body) > self.max_body_bytes:
            raise FetchError(f"body of {len(body)} bytes exceeds limit of {self.max_body_bytes}")
        return FetchResponse(
            url=str(response.url),
            status=int(response.status),
            headers={k.lower(): str(v) for k, v in dict(response.headers).items()},
            body=body,
            elapsed=time.perf_counter() - started,
        )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.__aexit__(None, None, None)
        self._session = self._client = None


class OfflineFetcher:
    """Refuses every request. Used by ``reparse`` to guarantee zero network I/O."""

    async def fetch(self, url: str, headers: dict[str, str]) -> FetchResponse:
        raise FetchError(f"offline mode: {url} is not in the raw store")

    async def close(self) -> None:
        return None
