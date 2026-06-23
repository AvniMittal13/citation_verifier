# citation_verifier

Verify and auto-correct a BibTeX file against scholarly metadata sources.

For every entry in your `.bib`, it looks up the paper, fetches the canonical
BibTeX, and compares it against your entry. If they describe the same paper but
your fields differ, it writes a corrected copy to a **separate file** and
reports exactly what changed.

Two backends (`--source`):

- **`api`** (default, recommended): free metadata APIs — **Crossref**, **arXiv**,
  and **Semantic Scholar**. No browser, no CAPTCHA, parallel-safe.
- **`scholar`**: Google Scholar via **Playwright** (Chromium). Real Scholar data
  + citation counts, but Google CAPTCHA-walls automated queries, so it pauses for
  you to solve them. Use only for a few entries at a time.

Matching: deterministic title/author similarity, with **GitHub Copilot CLI**
(`copilot -p ...`) consulted for borderline cases. Falls back automatically if
the CLI is unavailable. Your original `.bib` is **never modified**.

## Files

| File | Purpose |
| --- | --- |
| `main.py` | CLI + pipeline orchestration |
| `bibtex_io.py` | Load / parse / write BibTeX + dedup by citation ID |
| `providers.py` | CAPTCHA-free APIs (Crossref / arXiv / Semantic Scholar) |
| `scholar.py` | Google Scholar scraping via Playwright (fallback source) |
| `copilot_llm.py` | GitHub Copilot CLI integration |
| `matcher.py` | Same-paper decision + field diffing |
| `report.py` | Markdown report + console summary |

## Setup

```bash
cd citation_verifier
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

Optional (for LLM matching) — install/authenticate the GitHub Copilot CLI so
that `copilot` is on your `PATH`:

```bash
copilot --version   # should print a version; auth uses your Copilot login
```

If `copilot` is missing, the tool prints `Copilot CLI available: False` and uses
deterministic matching only.

## Usage

```bash
# Verify via free APIs (default source, no CAPTCHA)
python main.py references.bib

# Resume after an interruption from entry 14 (keeps prior corrections)
python main.py references.bib --start 14

# Only the first 3 entries (quick smoke test)
python main.py references.bib --limit 3

# Use Google Scholar instead (visible browser, solve CAPTCHAs)
python main.py references.bib --source scholar --headed

# Disable the Google Scholar fallback used for API not-found entries
python main.py references.bib --no-scholar-backup

# Re-verify automatically whenever you edit/save references.bib
python main.py references.bib --watch
```

### Source notes & limitations

- **Crossref** is fast and keyless but does not index many arXiv-only preprints,
  and can surface near-title false positives. A guard rejects sub-0.92-similarity
  matches unless the first-author surname is corroborated in the result.
- **Semantic Scholar** has the best AI/CS + arXiv coverage and returns ready-made
  BibTeX, but its search endpoint now rate-limits (HTTP 429) without a key. Set
  `SEMANTIC_SCHOLAR_API_KEY` to use it reliably:
  ```bash
  export SEMANTIC_SCHOLAR_API_KEY=your_key   # https://www.semanticscholar.org/product/api
  ```
- **arXiv** must be queried over `https://` and can throttle rapid programmatic
  requests; the provider fails fast so the others still produce a result.
- **Google Scholar** (`--source scholar`) rate-limits aggressively; keep batches
  small. When a CAPTCHA appears the run pauses until you solve it in the browser.

### Resilience

Outputs are saved **after every entry** and in a `finally` block, so a crash,
network timeout, or Ctrl-C never loses progress. Re-run with `--start N` to
resume.

### Outputs

Each run creates a fresh timestamped folder `results/<YYYY-MM-DD_HH-MM-SS>/`
next to your input file, containing:

- `references.verified.bib` — entries that were resolved (corrected in place,
  original citation keys preserved). **Not-found entries are excluded.**
- `not_found.bib` — entries that could NOT be resolved by any source, kept as
  their original BibTeX **for manual review** so you can fix them by hand.
- `report.md` — a table of every entry: status, similarity, source/method, and
  which fields changed.

### Google Scholar backup

When using the default `api` source, any entry the APIs cannot resolve is
automatically retried via **Google Scholar (Playwright)** before being marked
not-found. Run with `--headed` so you can solve a CAPTCHA if Scholar shows one;
in headless mode the backup reports the CAPTCHA and the entry goes to
`not_found.bib`. Disable the fallback with `--no-scholar-backup`.
