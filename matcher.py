"""Decide whether a Scholar entry matches and should replace a user entry.

Combines a deterministic title/author similarity check with an optional
GitHub Copilot CLI verdict. The deterministic check is always computed; the LLM
is consulted (when enabled and available) for borderline cases or to confirm.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Optional

import copilot_llm
from bibtex_io import COMPARABLE_FIELDS

# Title similarity thresholds.
SAME_THRESHOLD = 0.85          # >= this -> clearly the same paper
BORDERLINE_LOW = 0.55          # below this -> clearly different
# Between BORDERLINE_LOW and SAME_THRESHOLD we defer to the LLM if available.


@dataclass
class MatchDecision:
    same_paper: bool
    title_similarity: float
    method: str                       # "deterministic" or "llm" or "llm+fallback"
    changed_fields: list = field(default_factory=list)
    reason: str = ""


def _normalize(text: str) -> str:
    text = text.lower()
    text = text.replace("{", "").replace("}", "")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_similarity(a: dict, b: dict) -> float:
    ta, tb = _normalize(a.get("title", "")), _normalize(b.get("title", ""))
    if not ta or not tb:
        return 0.0
    return SequenceMatcher(None, ta, tb).ratio()


def _field_value(entry: dict, key: str) -> str:
    return _normalize(entry.get(key, ""))


def diff_fields(original: dict, scholar: dict) -> list:
    """Return the list of comparable fields whose normalized values differ."""
    changed = []
    for key in COMPARABLE_FIELDS:
        orig_val = _field_value(original, key)
        sch_val = _field_value(scholar, key)
        if sch_val and orig_val != sch_val:
            changed.append(key)
    return changed


def decide(original: dict, scholar: dict, use_llm: bool = True) -> MatchDecision:
    sim = title_similarity(original, scholar)
    changed = diff_fields(original, scholar)

    # Clear-cut deterministic cases.
    if sim >= SAME_THRESHOLD:
        same, method, reason = True, "deterministic", f"title similarity {sim:.2f}"
    elif sim < BORDERLINE_LOW:
        same, method, reason = False, "deterministic", f"title similarity {sim:.2f}"
    else:
        # Borderline: ask the LLM if allowed/available.
        verdict = copilot_llm.compare_entries(original, scholar) if use_llm else None
        if verdict is not None:
            same = verdict.same_paper
            method = "llm"
            reason = f"llm conf={verdict.confidence:.2f}: {verdict.reason}"
        else:
            # Fall back to a lenient threshold midpoint.
            same = sim >= 0.7
            method = "deterministic" if not use_llm else "llm+fallback"
            reason = f"fallback on title similarity {sim:.2f}"

    return MatchDecision(
        same_paper=same,
        title_similarity=sim,
        method=method,
        changed_fields=changed if same else [],
        reason=reason,
    )


def build_updated_entry(original: dict, scholar: dict) -> dict:
    """Produce the replacement entry: Scholar's fields, original citation key."""
    updated = dict(scholar)
    updated["ID"] = original.get("ID", scholar.get("ID", ""))
    return updated
