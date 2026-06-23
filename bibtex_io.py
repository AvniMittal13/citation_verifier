"""Load, parse and write BibTeX files.

Thin wrapper around ``bibtexparser`` (v1.x) so the rest of the codebase deals
with plain ``dict`` entries. Each entry dict uses the bibtexparser convention:

    {
        "ENTRYTYPE": "article",
        "ID": "vaswani2017attention",
        "title": "...",
        "author": "...",
        ...
    }
"""

from __future__ import annotations

import copy
from typing import Optional

import bibtexparser
from bibtexparser.bibdatabase import BibDatabase
from bibtexparser.bparser import BibTexParser
from bibtexparser.bwriter import BibTexWriter

# Fields we care about when comparing / displaying entries.
COMPARABLE_FIELDS = [
    "title",
    "author",
    "year",
    "journal",
    "booktitle",
    "volume",
    "number",
    "pages",
    "publisher",
    "doi",
    "month",
    "organization",
]


def _new_parser() -> BibTexParser:
    parser = BibTexParser(common_strings=True)
    parser.ignore_nonstandard_types = False
    parser.homogenize_fields = False
    return parser


def load_database(path: str):
    """Load a .bib file and return the bibtexparser database object."""
    with open(path, "r", encoding="utf-8") as handle:
        return bibtexparser.load(handle, parser=_new_parser())


def parse_bibtex_string(text: str) -> Optional[dict]:
    """Parse a single-entry BibTeX string (e.g. from Google Scholar export).

    Returns the first entry as a dict, or ``None`` if nothing parses.
    """
    if not text or "@" not in text:
        return None
    try:
        db = bibtexparser.loads(text, parser=_new_parser())
    except Exception:
        return None
    if not db.entries:
        return None
    return db.entries[0]


def write_database(db, path: str) -> None:
    """Serialise a bibtexparser database to disk, preserving entry order."""
    writer = BibTexWriter()
    writer.indent = "  "
    writer.order_entries_by = None  # keep original order
    with open(path, "w", encoding="utf-8") as handle:
        bibtexparser.dump(db, handle, writer=writer)


def clone_database(db):
    """Deep-copy a database so we can mutate the copy without touching input."""
    return copy.deepcopy(db)


def find_entry(db, entry_id: str) -> Optional[dict]:
    for entry in db.entries:
        if entry.get("ID") == entry_id:
            return entry
    return None


def deduplicate_by_id(db):
    """Remove duplicate entries that share the same citation ID (key).

    Keeps the first occurrence of each ID and drops later ones. Mutates ``db``
    in place (both ``db.entries`` and ``db.entries_dict`` where present) and
    returns a list of ``(entry_id, count_removed)`` tuples describing the
    duplicates that were dropped.
    """
    seen = set()
    deduped = []
    removed_counts = {}
    for entry in db.entries:
        entry_id = entry.get("ID")
        if entry_id in seen:
            removed_counts[entry_id] = removed_counts.get(entry_id, 0) + 1
            continue
        seen.add(entry_id)
        deduped.append(entry)

    db.entries = deduped
    # bibtexparser caches a dict view; rebuild it so it stays consistent.
    if hasattr(db, "_entries_dict"):
        db._entries_dict = {}

    return sorted(removed_counts.items())


def subset_database(db, ids, include: bool = True):
    """Return a NEW database containing a subset of ``db``'s entries.

    ``include=True``  -> keep only entries whose ID is in ``ids``.
    ``include=False`` -> keep only entries whose ID is NOT in ``ids``.

    The returned database has no comments/strings/preambles, so writing it does
    not emit stray ``@comment`` lines. Entry order is preserved.
    """
    ids = set(ids)
    new_db = bibtexparser.bibdatabase.BibDatabase()
    new_db.entries = [copy.deepcopy(e) for e in db.entries
                      if (e.get("ID") in ids) == include]
    return new_db

