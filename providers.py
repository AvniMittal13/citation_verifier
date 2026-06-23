"""CAPTCHA-free bibliographic providers.

Replaces the Google Scholar scraper with free scholarly metadata APIs that do
not require a browser, an API key, or CAPTCHA solving:

  1. Semantic Scholar Graph API  - best coverage for CS/AI papers + arXiv;
     returns ready-made BibTeX in ``citationStyles.bibtex``.
  2. Crossref REST API           - published papers/journals; BibTeX via
     DOI content negotiation.
  3. arXiv API                   - preprints; BibTeX synthesised from Atom XML.

For a given reference entry each provider is queried by title; the candidate
whose title best matches is selected. The first provider that yields a
confident match wins, otherwise the best across all providers is returned.

All requests use the stdlib (``urllib``) so there are no extra dependencies,
and 429/503 responses are retried with backoff.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

# A contact email makes Crossref/Semantic Scholar treat us as a "polite" client.
CONTACT = "citation-verifier@example.com"
USER_AGENT = f"citation_verifier/1.0 (mailto:{CONTACT})"

# Optional Semantic Scholar API key (https://www.semanticscholar.org/product/api).
# Without it, S2's search endpoint is heavily rate-limited; with it, reliable.
S2_API_KEY = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip()

DEFAULT_ORDER = ["semantic_scholar", "crossref", "arxiv"]
# Title similarity at/above which a candidate is accepted outright.
CONFIDENT_SIM = 0.92
# Between MIN_SIM and CONFIDENT_SIM a candidate is only accepted if the first
# author's surname also matches (guards against near-title false positives such
# as "Is Attention All You Need?" vs "Attention is all you need").
MIN_SIM = 0.60

# Semantic Scholar's unauthenticated pool allows ~1 request/sec. We self-throttle
# to avoid 429s (which otherwise cost long backoff sleeps and lost matches).
_S2_MIN_INTERVAL = 1.2
_s2_last_call = 0.0


def _s2_throttle():
    global _s2_last_call
    now = time.time()
    wait = _S2_MIN_INTERVAL - (now - _s2_last_call)
    if wait > 0:
        time.sleep(wait)
    _s2_last_call = time.time()


@dataclass
class ProviderResult:
    raw_bibtex: Optional[str]
    matched_title: Optional[str]
    source: Optional[str] = None
    error: Optional[str] = None
    similarity: float = 0.0


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _clean(value: str) -> str:
    return value.replace("{", "").replace("}", "").replace("\n", " ").strip()


def _normalize(text: str) -> str:
    text = text.lower().replace("{", "").replace("}", "")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _similarity(a: str, b: str) -> float:
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _first_author_surname(author_field: str) -> str:
    if not author_field:
        return ""
    first = author_field.split(" and ")[0].strip()
    if "," in first:
        return first.split(",")[0].strip()
    return first.split(" ")[-1].strip()


def _http(url: str, accept: str, timeout: int, retries: int = 4,
          extra_headers: Optional[dict] = None):
    """GET a URL, returning the decoded text body (or None on failure)."""
    last_err = None
    for attempt in range(retries):
        headers = {"User-Agent": USER_AGENT, "Accept": accept}
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:  # noqa: PERF203
            last_err = exc
            if exc.code in (429, 500, 502, 503):
                # Exponential-ish backoff; Semantic Scholar 429s clear quickly.
                time.sleep(3 * (attempt + 1))
                continue
            return None
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(1 + attempt)
    return None


# --------------------------------------------------------------------------- #
# Providers — each returns (raw_bibtex, matched_title, similarity) or None
# --------------------------------------------------------------------------- #

def _semantic_scholar(title: str, author: str, timeout: int):
    # Use the dedicated title-match endpoint: it returns the single closest
    # paper for a title query (with a ready-made BibTeX in citationStyles),
    # which is both more accurate and lighter than relevance search. Returns
    # HTTP 404 when there is no title match.
    base = "https://api.semanticscholar.org/graph/v1/paper/search/match"
    params = {
        "query": title,
        "fields": "title,year,authors,venue,externalIds,citationStyles",
    }
    headers = {"x-api-key": S2_API_KEY} if S2_API_KEY else None
    # Per the API tutorial, unauthenticated callers share one rate limit and
    # should self-throttle + back off. We throttle before each call; _http
    # backs off on 429. Keep retries modest so a sustained block fails over to
    # crossref/arxiv rather than stalling.
    retries = 5 if S2_API_KEY else 3
    _s2_throttle()
    text = _http(base + "?" + urllib.parse.urlencode(params),
                 "application/json", timeout, retries=retries,
                 extra_headers=headers)
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None

    papers = data.get("data") or []
    if not papers:
        return None
    paper = papers[0]
    cand_title = paper.get("title") or ""
    styles = paper.get("citationStyles") or {}
    bib = styles.get("bibtex")
    if not cand_title or not bib:
        return None
    sim = _similarity(title, cand_title)
    return (bib, cand_title, sim)


def _crossref(title: str, author: str, timeout: int):
    params = {"query.bibliographic": title, "rows": "5", "mailto": CONTACT}
    text = _http("https://api.crossref.org/works?" + urllib.parse.urlencode(params),
                 "application/json", timeout)
    if not text:
        return None
    try:
        items = json.loads(text)["message"]["items"]
    except (json.JSONDecodeError, KeyError):
        return None

    best = None
    for item in items or []:
        titles = item.get("title") or []
        cand_title = titles[0] if titles else ""
        doi = item.get("DOI")
        if not cand_title or not doi:
            continue
        sim = _similarity(title, cand_title)
        if best is None or sim > best[2]:
            best = (doi, cand_title, sim)
    if best is None:
        return None

    doi, cand_title, sim = best
    bib = _http(f"https://api.crossref.org/works/{urllib.parse.quote(doi)}"
                f"/transform/application/x-bibtex",
                "application/x-bibtex", timeout)
    if not bib or "@" not in bib:
        return None
    return (bib.strip(), cand_title, sim)


def _arxiv(title: str, author: str, timeout: int):
    # arXiv's "ti:" field search is finicky with punctuation/colons, so query a
    # cleaned title phrase across all fields and rank candidates by similarity.
    clean = re.sub(r"[^a-zA-Z0-9 ]+", " ", title)
    clean = re.sub(r"\s+", " ", clean).strip()
    params = {
        "search_query": f'all:{clean}',
        "max_results": "10",
        "sortBy": "relevance",
    }
    # arXiv's export API can be sluggish; keep the timeout/retries tight so a
    # slow response fails fast and lets the other providers carry the result.
    text = _http("https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params),
                 "application/atom+xml", min(timeout, 12), retries=2)
    if not text:
        return None
    ns = {"a": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None

    best = None
    for entry in root.findall("a:entry", ns):
        cand_title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
        cand_title = re.sub(r"\s+", " ", cand_title)
        if not cand_title:
            continue
        sim = _similarity(title, cand_title)
        if best is None or sim > best[1]:
            best = (entry, sim, cand_title)
    if best is None:
        return None

    entry, sim, cand_title = best
    authors = [a.findtext("a:name", default="", namespaces=ns)
               for a in entry.findall("a:author", ns)]
    published = entry.findtext("a:published", default="", namespaces=ns)
    year = published[:4] if published else ""
    arxiv_id = entry.findtext("a:id", default="", namespaces=ns)
    arxiv_id = arxiv_id.rsplit("/", 1)[-1] if arxiv_id else ""
    arxiv_id = re.sub(r"v\d+$", "", arxiv_id)  # strip version suffix

    author_str = " and ".join(a for a in authors if a)
    key = (_normalize(authors[0]).split(" ")[-1] if authors else "arxiv") + (year or "")
    bib = (
        f"@article{{{key},\n"
        f"  title={{{cand_title}}},\n"
        f"  author={{{author_str}}},\n"
        f"  journal={{arXiv preprint arXiv:{arxiv_id}}},\n"
        f"  year={{{year}}}\n"
        f"}}\n"
    )
    return (bib, cand_title, sim)


_PROVIDERS = {
    "semantic_scholar": _semantic_scholar,
    "crossref": _crossref,
    "arxiv": _arxiv,
}


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def build_query_title(entry: dict) -> str:
    return _clean(entry.get("title", ""))


def fetch_bibtex(entry: dict, timeout: int = 20,
                 order: Optional[list] = None) -> ProviderResult:
    """Look up ``entry`` across providers and return the best BibTeX match.

    All providers are queried and the candidate with the highest title
    similarity wins. We deliberately do NOT stop at the first provider: a later
    provider may hold the exact paper while an earlier one returned only a
    near-title false positive (e.g. "Is Attention All You Need?" vs "Attention
    is all you need").
    """
    title = _clean(entry.get("title", ""))
    author = _first_author_surname(entry.get("author", ""))
    if not title:
        return ProviderResult(None, None, error="entry has no title")

    order = order or DEFAULT_ORDER
    overall_best = None  # (bib, title, sim, source)

    for name in order:
        provider = _PROVIDERS.get(name)
        if provider is None:
            continue
        try:
            result = provider(title, author, timeout)
        except Exception as exc:  # noqa: BLE001
            result = None
            _ = exc
        if not result:
            continue
        bib, cand_title, sim = result
        if overall_best is None or sim > overall_best[2]:
            overall_best = (bib, cand_title, sim, name)
        # If one provider already has a (near-)exact match, no need to keep
        # querying the rest.
        if sim >= 0.97:
            break

    if overall_best is None:
        return ProviderResult(None, None, error="no provider returned a result")

    bib, cand_title, sim, name = overall_best
    if sim < MIN_SIM:
        return ProviderResult(None, cand_title, source=name, similarity=sim,
                              error=f"best match too weak (sim {sim:.2f})")

    # Guard against near-title false positives: below the confident threshold,
    # require the user's first-author surname to also appear in the candidate.
    if sim < CONFIDENT_SIM and author:
        surname = author.lower()
        if surname and surname not in _normalize(bib):
            return ProviderResult(
                None, cand_title, source=name, similarity=sim,
                error=(f"weak match (sim {sim:.2f}) and author '{author}' "
                       f"not corroborated"))

    return ProviderResult(raw_bibtex=bib, matched_title=cand_title,
                          source=name, similarity=sim)
