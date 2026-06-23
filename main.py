"""Orchestrates the verification pipeline and the CLI.

Flow per entry:
  1. build a Scholar query from the entry,
  2. scrape the first result's BibTeX export,
  3. parse it and decide (deterministic + optional Copilot CLI) if it's the
     same paper,
  4. if it is and fields differ, replace the entry (keeping its citation key)
     in a *copy* of the database,
  5. record an EntryResult for the report.

The original .bib is never modified. Updates are written to a separate output
file (default: <input>.verified.bib) and a Markdown report lists what changed.

Usage:
    python main.py references.bib
    python main.py references.bib --output refs.verified.bib --no-llm --headed
    python main.py references.bib --watch
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

from playwright.sync_api import sync_playwright

import bibtex_io
import copilot_llm
import matcher
import providers
import report
import scholar

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _default_output(input_path: str) -> str:
    base, ext = os.path.splitext(input_path)
    return f"{base}.verified{ext or '.bib'}"


def _default_report(input_path: str) -> str:
    base, _ = os.path.splitext(input_path)
    return f"{base}.report.md"


def verify(input_path: str, output_path: str, report_path: str,
           notfound_path: str = None, use_llm: bool = True, headed: bool = True,
           delay: float = 3.0, limit: int = 0, start: int = 1,
           source: str = "api", scholar_backup: bool = True) -> list:
    db_in = bibtex_io.load_database(input_path)

    # Step 0: deduplicate by citation ID (keep first occurrence).
    removed = bibtex_io.deduplicate_by_id(db_in)
    if removed:
        total_removed = sum(c for _, c in removed)
        print(f"[dedup] removed {total_removed} duplicate "
              f"entr{'y' if total_removed == 1 else 'ies'} by citation ID:")
        for entry_id, count in removed:
            print(f"    - {entry_id}: {count} duplicate"
                  f"{'s' if count != 1 else ''} dropped")
    else:
        print("[dedup] no duplicate citation IDs found")

    # When resuming (start > 1) and an output already exists, continue from it
    # so previously applied corrections are preserved.
    if start > 1 and os.path.exists(output_path):
        db_out = bibtex_io.load_database(output_path)
        print(f"[verify] resuming from entry {start}; "
              f"loaded existing output {output_path}")
    else:
        db_out = bibtex_io.clone_database(db_in)

    all_entries = db_in.entries
    if limit > 0:
        all_entries = all_entries[:limit]
    # Skip already-processed entries when resuming (1-based start).
    entries = all_entries[max(start - 1, 0):]

    print(f"[verify] {len(entries)} entr{'y' if len(entries) == 1 else 'ies'} "
          f"to process from {input_path}")
    print(f"[verify] source: {source}"
          + (" (+ Google Scholar backup for not-found)"
             if source == "api" and scholar_backup else ""))
    if use_llm:
        print(f"[verify] Copilot CLI available: {copilot_llm.is_available()}")

    results = []

    # Lazily-managed Playwright browser, shared by the scholar source and by the
    # scholar backup. Created only when first needed, torn down in finally.
    pw_state = {"pw": None, "browser": None, "context": None, "page": None}

    def _scholar_page():
        if pw_state["page"] is None:
            pw = sync_playwright().start()
            browser = pw.chromium.launch(headless=not headed)
            context = browser.new_context(user_agent=USER_AGENT, locale="en-US")
            pw_state.update(pw=pw, browser=browser, context=context,
                            page=context.new_page())
        return pw_state["page"]

    def _close_scholar():
        if pw_state["pw"] is not None:
            try:
                pw_state["context"].close()
                pw_state["browser"].close()
                pw_state["pw"].stop()
            except Exception:  # noqa: BLE001
                pass

    def _save_progress():
        """Persist current output + report; safe to call repeatedly.

        Entries that could not be resolved (status not_found) are written to a
        SEPARATE file (``notfound_path``) for manual review and excluded from
        the main verified output.
        """
        nf_ids = {r.entry_id for r in results
                  if r.status == report.STATUS_NOT_FOUND}
        verified_db = bibtex_io.subset_database(db_out, nf_ids, include=False)
        bibtex_io.write_database(verified_db, output_path)
        if notfound_path:
            notfound_db = bibtex_io.subset_database(db_out, nf_ids, include=True)
            bibtex_io.write_database(notfound_db, notfound_path)
        report.write_markdown(results, report_path, input_path, output_path)

    def _process_entry(idx, entry, fetch):
        """Look up one entry, decide, and update db_out. Returns nothing."""
        entry_id = entry.get("ID", f"entry{idx}")
        orig_title = providers._clean(entry.get("title", ""))
        print(f"\n[{idx}/{len(all_entries)}] {entry_id}")

        try:
            raw_bibtex, matched_title, found_detail = fetch(entry)
        except Exception as exc:  # noqa: BLE001
            print(f"    ERROR    : {exc}")
            results.append(report.EntryResult(
                entry_id, orig_title, report.STATUS_ERROR, detail=str(exc)))
            return

        if not raw_bibtex:
            print(f"    not found: {found_detail or 'no BibTeX'}")
            results.append(report.EntryResult(
                entry_id, orig_title, report.STATUS_NOT_FOUND,
                matched_title=matched_title,
                detail=found_detail or "no result"))
            return

        scholar_entry = bibtex_io.parse_bibtex_string(raw_bibtex)
        if scholar_entry is None:
            print("    not found: could not parse fetched BibTeX")
            results.append(report.EntryResult(
                entry_id, orig_title, report.STATUS_NOT_FOUND,
                matched_title=matched_title,
                detail="unparseable BibTeX"))
            return

        print(f"    found    : {providers._clean(scholar_entry.get('title', ''))}")
        decision = matcher.decide(entry, scholar_entry, use_llm=use_llm)
        print(f"    decision : same_paper={decision.same_paper} "
              f"sim={decision.title_similarity:.2f} ({decision.method})")

        if not decision.same_paper:
            results.append(report.EntryResult(
                entry_id, orig_title, report.STATUS_MISMATCH,
                matched_title=scholar_entry.get("title"),
                similarity=decision.title_similarity, method=decision.method,
                detail=decision.reason))
        elif decision.changed_fields:
            updated = matcher.build_updated_entry(entry, scholar_entry)
            target = bibtex_io.find_entry(db_out, entry_id)
            if target is not None:
                target.clear()
                target.update(updated)
            print(f"    UPDATED  : {', '.join(decision.changed_fields)}")
            results.append(report.EntryResult(
                entry_id, orig_title, report.STATUS_UPDATED,
                matched_title=scholar_entry.get("title"),
                similarity=decision.title_similarity, method=decision.method,
                changed_fields=decision.changed_fields, detail=decision.reason))
        else:
            print("    verified : already correct")
            results.append(report.EntryResult(
                entry_id, orig_title, report.STATUS_VERIFIED,
                matched_title=scholar_entry.get("title"),
                similarity=decision.title_similarity, method=decision.method,
                detail="no field changes"))

    def _fetch_scholar(entry):
        query = scholar.build_query(entry)
        print(f"    query    : {query}")
        res = scholar.fetch_scholar_bibtex(_scholar_page(), query, headed=headed)
        return res.raw_bibtex, res.matched_title, res.error

    def _fetch_api(entry):
        print(f"    title    : {providers._clean(entry.get('title',''))}")
        res = providers.fetch_bibtex(entry)
        if res.source:
            print(f"    provider : {res.source} (sim {res.similarity:.2f})")
        if res.raw_bibtex:
            return res.raw_bibtex, res.matched_title, res.error
        # API could not resolve it -> optionally fall back to Google Scholar.
        if scholar_backup:
            print("    backup   : trying Google Scholar (Playwright)...")
            try:
                sres = scholar.fetch_scholar_bibtex(
                    _scholar_page(), scholar.build_query(entry), headed=headed)
            except Exception as exc:  # noqa: BLE001
                return None, res.matched_title, f"api: {res.error}; scholar: {exc}"
            if sres.raw_bibtex:
                print("    backup ok: resolved via Google Scholar")
                return sres.raw_bibtex, sres.matched_title, sres.error
            return (None, sres.matched_title or res.matched_title,
                    f"api: {res.error}; scholar: {sres.error}")
        return None, res.matched_title, res.error

    fetch = _fetch_scholar if source == "scholar" else _fetch_api

    try:
        for offset, entry in enumerate(entries):
            idx = max(start, 1) + offset
            _process_entry(idx, entry, fetch)
            _save_progress()
            if delay:
                time.sleep(delay)
    except KeyboardInterrupt:
        print("\n[verify] Interrupted by user; saving partial progress...")
    except Exception as exc:  # noqa: BLE001
        print(f"\n[verify] Run aborted ({exc}); saving partial progress...")
    finally:
        _close_scholar()
        _save_progress()

    n_notfound = sum(1 for r in results if r.status == report.STATUS_NOT_FOUND)
    report.print_summary(results)
    print(f"\n[verify] Verified .bib written to : {output_path}")
    if notfound_path and n_notfound:
        print(f"[verify] Not-found (review) written to: {notfound_path} "
              f"({n_notfound} entr{'y' if n_notfound == 1 else 'ies'})")
    print(f"[verify] Report written to        : {report_path}")
    print(f"[verify] Processed {len(results)}/{len(entries)} entries this run.")
    return results


def _run_once(args) -> None:
    # Each run writes into results/<YYYY-MM-DD_HH-MM-SS>/ next to the input,
    # unless an explicit --output is given.
    if args.output:
        output_path = args.output
        report_path = args.report or _default_report(args.input)
        base, ext = os.path.splitext(output_path)
        notfound_path = f"{base}.not_found{ext or '.bib'}"
        run_dir = os.path.dirname(output_path) or "."
    else:
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        input_dir = os.path.dirname(os.path.abspath(args.input))
        run_dir = os.path.join(input_dir, "results", ts)
        os.makedirs(run_dir, exist_ok=True)
        output_path = os.path.join(run_dir, "references.verified.bib")
        notfound_path = os.path.join(run_dir, "not_found.bib")
        report_path = args.report or os.path.join(run_dir, "report.md")

    print(f"[run] output folder: {run_dir}")
    verify(
        input_path=args.input,
        output_path=output_path,
        report_path=report_path,
        notfound_path=notfound_path,
        use_llm=not args.no_llm,
        headed=args.headed,
        delay=args.delay,
        limit=args.limit,
        start=args.start,
        source=args.source,
        scholar_backup=not args.no_scholar_backup,
    )


def _watch(args) -> None:
    print(f"[watch] Watching {args.input} for changes (Ctrl+C to stop)...")
    last_mtime = None
    try:
        while True:
            try:
                mtime = os.path.getmtime(args.input)
            except OSError:
                mtime = None
            if mtime is not None and mtime != last_mtime:
                if last_mtime is not None:
                    print(f"[watch] Change detected in {args.input}, re-verifying...")
                last_mtime = mtime
                _run_once(args)
            time.sleep(2)
    except KeyboardInterrupt:
        print("\n[watch] Stopped.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify a BibTeX file against Google Scholar exports.")
    parser.add_argument("input", help="Path to the reference .bib file")
    parser.add_argument("-o", "--output", default=None,
                        help="Output .bib path (default: <input>.verified.bib)")
    parser.add_argument("-r", "--report", default=None,
                        help="Report .md path (default: <input>.report.md)")
    parser.add_argument("--no-llm", action="store_true",
                        help="Disable Copilot CLI matching (deterministic only)")
    parser.add_argument("--source", choices=["api", "scholar"], default="api",
                        help="Metadata source: 'api' (Semantic Scholar/Crossref/"
                             "arXiv, no CAPTCHA; default) or 'scholar' (Google "
                             "Scholar via browser)")
    parser.add_argument("--no-scholar-backup", action="store_true",
                        help="Disable the Google Scholar (Playwright) fallback "
                             "used for entries the APIs cannot resolve")
    parser.add_argument("--headed", dest="headed", action="store_true", default=True,
                        help="Run the browser visibly (scholar source only)")
    parser.add_argument("--headless", dest="headed", action="store_false",
                        help="Run the browser headless (scholar source only)")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="Seconds to wait between entries (default: 1.0)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Only process the first N entries (0 = all)")
    parser.add_argument("--start", type=int, default=1,
                        help="Resume from this 1-based entry index (preserves "
                             "prior output)")
    parser.add_argument("--watch", action="store_true",
                        help="Watch the input file and re-verify on changes")
    args = parser.parse_args(argv)

    if not os.path.exists(args.input):
        print(f"error: input file not found: {args.input}", file=sys.stderr)
        return 2

    if args.watch:
        _watch(args)
    else:
        _run_once(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
