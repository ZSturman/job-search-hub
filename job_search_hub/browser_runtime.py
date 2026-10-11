"""Small, shared Playwright runtime for public, read-only job discovery.

The pipeline has several browser consumers.  Keeping the lifecycle here means
one Chromium process/context can be reused by ingest, direct-career discovery,
and posting verification during a committed tick.  The runtime deliberately
does not expose form filling, login, or cookie persistence APIs.
"""

from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import dataclass


@dataclass(frozen=True)
class BrowserPage:
    """A rendered public page and the minimum diagnostic metadata needed."""

    url: str
    html: str
    http_status: int | None


class BrowserRuntime:
    """Lazily start one public, headless Chromium context.

    Failure is represented as a concise health message rather than an
    exception, so one unavailable browser cannot prevent API/RSS adapters
    from running.  Individual pages are always closed after extraction.
    """

    def __init__(self, *, user_agent: str = "JobSearchHubJobDiscovery/1.0") -> None:
        self.user_agent = user_agent
        self._playwright = None
        self._browser = None
        self._context = None
        self._error = ""

    @property
    def available(self) -> bool:
        return self._context is not None

    @property
    def error(self) -> str:
        return self._error

    def start(self) -> bool:
        if self.available:
            return True
        if self._error:
            return False
        try:
            from playwright.sync_api import sync_playwright

            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
            self._context = self._browser.new_context(user_agent=self.user_agent)
            return True
        except Exception as exc:  # import, executable, or launch errors
            self._error = (
                f"Browser unavailable ({type(exc).__name__}). "
                "Install the browser extra and Chromium in the jobhub environment; "
                "see docs/CONFIGURATION.md."
            )
            self.close()
            return False

    def render(
        self,
        url: str,
        *,
        timeout_seconds: float = 15,
        load_more: int = 0,
    ) -> BrowserPage | None:
        """Read a public listing/page, optionally revealing bounded extra rows."""
        if self.available:
            # A single unavailable source must not make a healthy shared
            # context look unavailable to the next adapter.
            self._error = ""
        if not self.start():
            return None
        page = None
        try:
            page = self._context.new_page()
            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=max(1, int(timeout_seconds * 1000)),
            )
            # A load-more click is a read-only listing expansion, bounded to
            # avoid turning a board into an uncontrolled crawler.
            for _ in range(max(0, load_more)):
                button = page.get_by_role(
                    "button", name=re.compile(r"load more|show more|more jobs", re.I)
                )
                if button.count() < 1:
                    break
                button.first.click(timeout=2_000)
                page.wait_for_timeout(250)
            return BrowserPage(
                url=page.url,
                html=page.content(),
                http_status=response.status if response is not None else None,
            )
        except Exception as exc:
            self._error = f"browser read failed: {type(exc).__name__}"
            return None
        finally:
            if page is not None:
                with suppress(Exception):
                    page.close()

    def close(self) -> None:
        if self._context is not None:
            with suppress(Exception):
                self._context.close()
        if self._browser is not None:
            with suppress(Exception):
                self._browser.close()
        if self._playwright is not None:
            with suppress(Exception):
                self._playwright.stop()
        self._context = self._browser = self._playwright = None

    def __enter__(self) -> BrowserRuntime:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
