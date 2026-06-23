"""Google Scholar scraping via Playwright.

For a given search query this module:
  1. opens the Scholar results page,
  2. finds the first result and clicks its "Cite" button,
  3. reads the "BibTeX" export link from the cite dialog,
  4. fetches the raw BibTeX text for that link.

Google Scholar aggressively rate-limits and shows CAPTCHAs. When one is
detected the scraper pauses and (in headed mode) waits for the human to solve
it in the visible browser window before continuing.
"""

from __future__ import annotations

import sys
import time
import urllib.parse
from dataclasses import dataclass
from typing import Optional

from playwright.sync_api import Page, TimeoutError as PWTimeout

SCHOLAR_SEARCH = "https://scholar.google.com/scholar?hl=en&q={q}"


@dataclass
class ScholarResult:
    raw_bibtex: Optional[str]
    matched_title: Optional[str]
    error: Optional[str] = None


def build_query(entry: dict) -> str:
    """Build a Scholar search string from a bib entry.

    Uses the quoted title plus the first author's surname. The year is
    deliberately omitted: including it tends to collapse Scholar results into a
    single "[CITATION]" stub (a citation-only record with poor metadata) rather
    than the real indexed paper.
    """
    title = _clean(entry.get("title", ""))
    author = _first_author(entry.get("author", ""))
    parts = []
    if title:
        parts.append(f'"{title}"')
    if author:
        parts.append(author)
    return " ".join(parts)


def _clean(value: str) -> str:
    return value.replace("{", "").replace("}", "").replace("\n", " ").strip()


def _first_author(author_field: str) -> str:
    if not author_field:
        return ""
    first = author_field.split(" and ")[0].strip()
    # "Surname, Given" -> "Surname"; "Given Surname" -> "Surname"
    if "," in first:
        return first.split(",")[0].strip()
    return first.split(" ")[-1].strip()


def _looks_like_captcha(page: Page) -> bool:
    url = page.url or ""
    if "/sorry/" in url or "ipv4" in url:
        return True
    try:
        body = page.inner_text("body", timeout=2000).lower()
    except Exception:
        return False
    needles = ["unusual traffic", "not a robot", "captcha", "recaptcha",
               "show you're", "verify you"]
    return any(n in body for n in needles)


def _handle_captcha(page: Page, headed: bool, max_wait: int = 600) -> None:
    """Wait for a human to solve a CAPTCHA in the visible browser.

    Instead of requiring a keypress on stdin (which is unavailable when the
    process runs in the background with redirected output), this polls the page
    until the CAPTCHA / blocking content disappears, or until ``max_wait``
    seconds elapse. Solve it directly in the browser window and the run resumes
    automatically.
    """
    print(
        "\n[scholar] CAPTCHA / blocking page detected. "
        "Please solve it in the browser window — the run will resume "
        "automatically once it clears.",
        file=sys.stderr,
        flush=True,
    )
    if not headed:
        raise RuntimeError(
            "CAPTCHA encountered in headless mode. Re-run with --headed to solve it."
        )

    waited = 0
    poll = 3
    while waited < max_wait:
        time.sleep(poll)
        waited += poll
        if not _looks_like_captcha(page):
            print(f"[scholar] CAPTCHA cleared after ~{waited}s; resuming.",
                  file=sys.stderr, flush=True)
            return
        if waited % 30 == 0:
            print(f"[scholar] still waiting for CAPTCHA to be solved "
                  f"(~{waited}s elapsed)...", file=sys.stderr, flush=True)

    raise RuntimeError(
        f"CAPTCHA not solved within {max_wait}s; aborting this entry.")


def fetch_scholar_bibtex(page: Page, query: str, headed: bool = True,
                         nav_timeout: int = 30000) -> ScholarResult:
    """Search Scholar for ``query`` and return the first result's BibTeX."""
    url = SCHOLAR_SEARCH.format(q=urllib.parse.quote_plus(query))
    try:
        page.goto(url, timeout=nav_timeout, wait_until="domcontentloaded")
    except PWTimeout:
        return ScholarResult(None, None, error="timeout loading search page")

    if _looks_like_captcha(page):
        _handle_captcha(page, headed)

    # Wait for the result list to appear. If it never shows, it is often a
    # CAPTCHA/block page that hadn't fully rendered during the first check, so
    # re-check and let the human solve it, then retry once.
    results = page.locator("div.gs_r.gs_or")
    try:
        results.first.wait_for(timeout=nav_timeout)
    except PWTimeout:
        if _looks_like_captcha(page):
            _handle_captcha(page, headed)
            try:
                results.first.wait_for(timeout=nav_timeout)
            except PWTimeout:
                return ScholarResult(None, None, error="no results found")
        else:
            return ScholarResult(None, None, error="no results found")

    # Pick the first *real* result: skip "[CITATION]" stubs (citation-only
    # records with poor metadata, e.g. a single malformed author and no Cite
    # export). Such titles are prefixed with "[CITATION]" by Scholar.
    chosen = None
    matched_title = None
    count = results.count()
    for i in range(count):
        block = results.nth(i)
        try:
            raw_title = block.locator("h3.gs_rt").first.inner_text(timeout=5000)
        except Exception:
            raw_title = ""
        if "[CITATION]" in raw_title.upper():
            continue
        # A usable result must expose a "Cite" button.
        if block.locator("a.gs_or_cit").count() == 0:
            continue
        chosen = block
        matched_title = (raw_title.replace("[PDF]", "").replace("[HTML]", "")
                         .strip())
        break

    if chosen is None:
        return ScholarResult(None, None,
                             error="no citable result (only [CITATION] stubs)")

    # Click the "Cite" button on the chosen result.
    cite_button = chosen.locator("a.gs_or_cit").first
    try:
        cite_button.click(timeout=nav_timeout)
    except PWTimeout:
        return ScholarResult(None, matched_title, error="cite button not found")

    # The cite dialog loads export links (BibTeX / EndNote / ...).
    try:
        page.locator("#gs_citi a.gs_citi").first.wait_for(timeout=nav_timeout)
    except PWTimeout:
        if _looks_like_captcha(page):
            _handle_captcha(page, headed)
            try:
                page.locator("#gs_citi a.gs_citi").first.wait_for(timeout=nav_timeout)
            except PWTimeout:
                return ScholarResult(None, matched_title, error="cite dialog did not load")
        else:
            return ScholarResult(None, matched_title, error="cite dialog did not load")

    bib_link = None
    links = page.locator("#gs_citi a.gs_citi")
    for i in range(links.count()):
        link = links.nth(i)
        text = (link.inner_text() or "").strip().lower()
        if text == "bibtex":
            bib_link = link
            break
    if bib_link is None:
        return ScholarResult(None, matched_title, error="BibTeX export link not found")

    bib_href = bib_link.get_attribute("href")

    # Actually click the "BibTeX" link (as a human would) and read the BibTeX
    # text shown on the resulting export page. Clicking navigates the page to
    # Scholar's scholar.bib endpoint, which renders the raw BibTeX as plain
    # text in the document body.
    raw = None
    try:
        bib_link.click(timeout=nav_timeout)
        page.wait_for_load_state("domcontentloaded", timeout=nav_timeout)
        if _looks_like_captcha(page):
            _handle_captcha(page, headed)
            page.wait_for_load_state("domcontentloaded", timeout=nav_timeout)
        body = page.inner_text("body", timeout=nav_timeout)
        if body and "@" in body:
            raw = body.strip()
    except Exception:
        raw = None

    # Fallback: fetch the export URL directly via the browser session.
    if not raw and bib_href:
        try:
            response = page.context.request.get(bib_href, timeout=nav_timeout)
            if response.ok:
                text = response.text()
                if "@" in text:
                    raw = text.strip()
        except Exception:  # noqa: BLE001
            raw = None

    if not raw:
        return ScholarResult(None, matched_title,
                             error="could not read BibTeX from export page")

    return ScholarResult(raw_bibtex=raw, matched_title=matched_title)
