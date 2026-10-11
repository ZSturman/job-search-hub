"""Public, read-only Playwright adapters for job boards without stable APIs."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from urllib.parse import unquote, urljoin, urlsplit

from job_search_hub.browser_runtime import BrowserRuntime
from job_search_hub.config import Settings
from job_search_hub.ingest.base import Adapter, RawPosting, SearchRequest, SearchResult
from job_search_hub.ingest.direct_discovery import (
    discover_target,
    extract_embedded_jobs,
    extract_html_job_links,
    extract_jsonld_postings,
)
from job_search_hub.ingest.preview import skipped_result


@dataclass(frozen=True)
class BrowserBoard:
    key: str
    label: str
    url: str
    recipe: str = "public_board"
    load_more: int = 2


# These are public listing pages verified as anonymous discovery targets.  The
# extractor prefers structured data and uses conservative listing links only
# when a board does not expose a public JSON/API contract.
BOARDS: tuple[BrowserBoard, ...] = (
    BrowserBoard("wellfound", "Wellfound", "https://wellfound.com/jobs"),
    BrowserBoard("weloveproduct", "WeLoveProduct", "https://weloveproduct.co/"),
    BrowserBoard(
        "mind_the_product", "Mind the Product Jobs", "https://www.mindtheproduct.com/jobs/"
    ),
    BrowserBoard("dribbble", "Dribbble Jobs", "https://dribbble.com/jobs"),
    BrowserBoard("levels_fyi", "Levels.fyi Jobs", "https://www.levels.fyi/jobs"),
    BrowserBoard("pocit", "POCIT Jobs", "https://www.pocitjobs.com/"),
    BrowserBoard("y_combinator", "Y Combinator Jobs", "https://www.ycombinator.com/jobs"),
    BrowserBoard(
        "resources_for_humans", "Resources for Humans / Lattice", "https://hrjobs.lattice.com/"
    ),
    BrowserBoard("built_in", "Built In", "https://builtin.com/jobs"),
    BrowserBoard("the_muse", "The Muse", "https://www.themuse.com/search/jobs"),
    BrowserBoard("remote_com", "Remote.com Jobs", "https://remote.com/jobs"),
    BrowserBoard(
        "welcome_to_the_jungle",
        "Welcome to the Jungle",
        "https://www.welcometothejungle.com/en/jobs",
    ),
    BrowserBoard(
        "people_ops_job_board", "The People Ops Job Board", "https://www.thepeopleopsjobboard.com/"
    ),
    BrowserBoard("blind", "Blind Jobs", "https://www.teamblind.com/jobs"),
    BrowserBoard(
        "index_ventures",
        "Index Ventures Portfolio Jobs",
        "https://www.indexventures.com/startup-jobs",
    ),
    BrowserBoard(
        "earlybird", "Earlybird Portfolio Jobs", "https://jobs.earlybird.com/jobs", "getro"
    ),
    BrowserBoard(
        "general_catalyst",
        "General Catalyst Portfolio Jobs",
        "https://jobs.generalcatalyst.com/jobs",
        "getro",
    ),
    BrowserBoard(
        "speedinvest",
        "Speedinvest Portfolio Jobs",
        "https://careers.speedinvest.com/jobs",
        "getro",
    ),
    BrowserBoard("planet_a", "Planet A Portfolio Jobs", "https://jobs.planet-a.com/jobs", "getro"),
    BrowserBoard("antler", "Antler Portfolio Jobs", "https://careers.antler.co/jobs", "getro"),
    BrowserBoard(
        "creandum", "Creandum Portfolio Jobs", "https://jobs.creandum.com/jobs", "consider"
    ),
    BrowserBoard(
        "kleiner_perkins",
        "Kleiner Perkins Portfolio Jobs",
        "https://jobs.kleinerperkins.com/jobs",
        "consider",
    ),
    BrowserBoard("work_with_indies", "Work With Indies", "https://www.workwithindies.com/"),
    BrowserBoard("career_vault", "Career Vault", "https://careervault.io/"),
    BrowserBoard("escape_the_city", "Escape the City", "https://www.escapethecity.org/search/jobs"),
    BrowserBoard("open_to_work_remote", "Open to Work Remote", "https://opentoworkremote.com/"),
    BrowserBoard(
        "experimentation_jobs", "Experimentation Jobs", "https://experimentationjobs.com/"
    ),
    BrowserBoard("people_first_jobs", "People-First Jobs", "https://peoplefirstjobs.com/jobs"),
    BrowserBoard("people_ops_jobs", "People Operations Jobs", "https://peopleopsjobs.io/"),
    BrowserBoard("fractional_jobs", "Fractional Jobs", "https://fractionaljobs.io/"),
    BrowserBoard(
        "product_jobs_anywhere", "Product Jobs Anywhere", "https://productjobsanywhere.com/"
    ),
    BrowserBoard("elixir_jobs", "Elixir Jobs", "https://elixirjobs.net/"),
    BrowserBoard("design_systems_jobs", "Design Systems Jobs", "https://designsystems.jobs/"),
)


def _dedupe(postings: Iterable[RawPosting]) -> list[RawPosting]:
    output: list[RawPosting] = []
    seen: set[str] = set()
    for posting in postings:
        key = f"{posting.url.casefold()}\0{posting.role_title.casefold()}"
        if key and key not in seen:
            seen.add(key)
            output.append(posting)
    return output


_NAVIGATION_TITLES = {
    "log in",
    "login",
    "sign up",
    "signup",
    "find a job",
    "search jobs",
    "view jobs",
    "all jobs",
}


def _is_listing_noise(posting: RawPosting, board: BrowserBoard, final_url: str) -> bool:
    """Reject navigation/auth/filter links that happen to contain ``job``."""
    title = " ".join((posting.role_title or "").casefold().split())
    parsed = urlsplit(posting.url)
    path = parsed.path.rstrip("/").casefold()
    listing_path = urlsplit(final_url).path.rstrip("/").casefold()
    host = parsed.netloc.casefold()
    yc_category = board.key == "y_combinator" and (
        "/jobs/role/" in path or "/jobs/category/" in path or "/resume" in path
    )
    return bool(
        title in _NAVIGATION_TITLES
        or any(part in host for part in ("account.", "auth."))
        or any(
            marker in path
            for marker in (
                "/login",
                "/authenticate",
                "/sign-up",
                "/signup",
                "/resume",
                "/search",
                "/category/",
                "/role/",
            )
        )
        or path in {"", "/", "/jobs", "/careers", listing_path}
        or path.startswith("/jobs.")
        or yc_category
    )


def _company_from_board_url(board: BrowserBoard, posting: RawPosting, fallback: str) -> str:
    if board.key == "y_combinator":
        match = re.search(r"/companies/([^/]+)/jobs/", urlsplit(posting.url).path, re.I)
        if match:
            return unquote(match.group(1)).replace("-", " ").strip().title()
    return fallback


def _board_postings(board: BrowserBoard, *, html: str, final_url: str) -> list[RawPosting]:
    """Normalize public rendered data into the project's existing envelope."""
    postings = extract_jsonld_postings(html, base_url=final_url)
    postings = postings or extract_embedded_jobs(html, base_url=final_url)
    postings = postings or extract_html_job_links(html, base_url=final_url)
    host = urlsplit(final_url).hostname or board.label
    for posting in postings:
        if _is_listing_noise(posting, board, final_url):
            continue
        posting.source = board.label
        posting.source_adapter = board.key
        # Link-only boards cannot reliably expose company in each card.  Keep
        # the page host as an explicit conservative fallback rather than
        # inventing a company name.
        if not posting.company or posting.company == (urlsplit(final_url).hostname or ""):
            posting.company = _company_from_board_url(board, posting, host)
        if not posting.external_job_id:
            posting.external_job_id = sha256(posting.url.encode()).hexdigest()[:24]
        posting.extras = {
            **posting.extras,
            "strategy": f"playwright_{board.recipe}",
            "discovery_url": board.url,
            "rendered": True,
            "source_quality": "public_job_board",
        }
    kept = (posting for posting in postings if not _is_listing_noise(posting, board, final_url))
    return _dedupe(kept)


class BrowserBoardAdapter(Adapter):
    """One configured public board, using the tick-owned browser runtime."""

    def __init__(self, settings: Settings, board: BrowserBoard) -> None:
        self.settings = settings
        self.board = board
        self.key = board.key
        self.label = board.label
        self.browser_runtime: BrowserRuntime | None = None
        self.last_diagnostic: dict[str, object] = {}
        super().__init__()

    def plan_searches(self) -> Iterable[SearchRequest]:
        yield SearchRequest(
            source=self.label,
            source_adapter=self.key,
            endpoint=self.board.url,
            target=self.board.url,
            target_kind="public_job_board",
        )

    def fetch(self) -> Iterable[RawPosting]:
        runtime = self.browser_runtime or BrowserRuntime()
        owns_runtime = self.browser_runtime is None
        try:
            rendered = runtime.render(
                self.board.url,
                timeout_seconds=self.settings.direct_discovery.timeout_seconds,
                load_more=self.board.load_more,
            )
            if rendered is None:
                self.last_diagnostic = {
                    "status": "browser_unavailable",
                    "url": self.board.url,
                    "error": runtime.error,
                    "postings": 0,
                }
                return []
            if rendered.http_status in {401, 403, 429}:
                self.last_diagnostic = {
                    "status": "access_restricted",
                    "url": rendered.url,
                    "http_status": rendered.http_status,
                    "error": "anonymous public listing access restricted",
                    "postings": 0,
                }
                return []
            if rendered.http_status is not None and rendered.http_status >= 400:
                self.last_diagnostic = {
                    "status": "http_error",
                    "url": rendered.url,
                    "http_status": rendered.http_status,
                    "postings": 0,
                }
                return []
            postings = _board_postings(self.board, html=rendered.html, final_url=rendered.url)
            self.last_diagnostic = {
                "status": "ok" if postings else "public_listing_not_exposed",
                "url": rendered.url,
                "http_status": rendered.http_status,
                "postings": len(postings),
                "recipe": self.board.recipe,
            }
            return postings
        finally:
            if owns_runtime:
                runtime.close()

    def preview(self, *, examples: int = 3) -> Iterable[SearchResult]:
        # Preview stays read-only but does not launch a browser.  This keeps
        # `preview-searches` deterministic and leaves live diagnostics to the
        # source check or committed ingestion run.
        yield skipped_result(
            source=self.label,
            source_adapter=self.key,
            reason="public browser board; run source-check --live or ingest",
        )


class WorkWithLinearAdapter(BrowserBoardAdapter):
    """Use Work With Linear only to discover public employer career targets."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(
            settings,
            BrowserBoard(
                "work_with_linear",
                "Work With Linear",
                "https://workwithlinear.com/",
                "career_directory",
                1,
            ),
        )

    def fetch(self) -> Iterable[RawPosting]:
        runtime = self.browser_runtime or BrowserRuntime()
        owns_runtime = self.browser_runtime is None
        try:
            rendered = runtime.render(
                self.board.url,
                timeout_seconds=self.settings.direct_discovery.timeout_seconds,
                load_more=self.board.load_more,
            )
            if rendered is None:
                self.last_diagnostic = {"status": "browser_unavailable", "error": runtime.error}
                return []
            # Reuse direct discovery only for concrete external public
            # careers links; the directory itself never becomes a job row.
            targets = []
            for href in re.findall(r"""href=["']([^"'#]+)["']""", rendered.html, flags=re.I):
                url = urljoin(rendered.url, href)
                parsed = urlsplit(url)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    continue
                path = parsed.path.casefold()
                if "workwithlinear.com" not in parsed.netloc.casefold() and any(
                    word in path for word in ("career", "job", "opening")
                ):
                    targets.append(url)
            output: list[RawPosting] = []
            for target in list(dict.fromkeys(targets))[:20]:
                result = discover_target(
                    target,
                    settings=self.settings.direct_discovery,
                    browser_runtime=runtime,
                )
                for posting in result.postings:
                    posting.source = self.label
                    posting.source_adapter = self.key
                    posting.extras = {**posting.extras, "directory_url": self.board.url}
                    output.append(posting)
            self.last_diagnostic = {
                "status": "ok",
                "url": rendered.url,
                "career_targets": len(targets),
                "postings": len(output),
            }
            return _dedupe(output)
        finally:
            if owns_runtime:
                runtime.close()
