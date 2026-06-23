"""LLM matching via the GitHub Copilot CLI.

Uses the standalone ``copilot`` CLI in non-interactive mode
(``copilot -p "<prompt>"``) which authenticates through your existing GitHub
Copilot session. The CLI is asked to return strict JSON deciding whether two
BibTeX entries describe the same paper.

If the CLI is missing, errors, or returns unparseable output, callers fall back
to deterministic matching (see ``matcher.py``).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

COPILOT_BIN = "copilot"


@dataclass
class LLMVerdict:
    same_paper: bool
    confidence: float
    reason: str


def is_available() -> bool:
    return shutil.which(COPILOT_BIN) is not None


def _build_prompt(original: dict, scholar: dict) -> str:
    def fmt(entry: dict) -> str:
        keys = ("title", "author", "year", "journal", "booktitle", "doi")
        return "\n".join(f"  {k}: {entry.get(k, '')}" for k in keys)

    return (
        "You are verifying bibliographic references. Decide whether the two "
        "BibTeX entries below refer to the SAME academic paper (ignore minor "
        "formatting, abbreviation or casing differences).\n\n"
        "ENTRY A (user's reference):\n" + fmt(original) + "\n\n"
        "ENTRY B (Google Scholar export):\n" + fmt(scholar) + "\n\n"
        "Respond with ONLY a single-line JSON object, no markdown, of the form:\n"
        '{"same_paper": true, "confidence": 0.0-1.0, "reason": "short reason"}'
    )


def _extract_json(text: str) -> Optional[dict]:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def compare_entries(original: dict, scholar: dict,
                    timeout: int = 60) -> Optional[LLMVerdict]:
    """Ask Copilot CLI whether the two entries are the same paper.

    Returns ``None`` on any failure so the caller can fall back.
    """
    if not is_available():
        return None

    prompt = _build_prompt(original, scholar)
    try:
        proc = subprocess.run(
            [COPILOT_BIN, "-p", prompt, "--allow-all-tools"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None

    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    data = _extract_json(output)
    if not data or "same_paper" not in data:
        return None

    try:
        return LLMVerdict(
            same_paper=bool(data["same_paper"]),
            confidence=float(data.get("confidence", 0.5)),
            reason=str(data.get("reason", "")).strip(),
        )
    except (TypeError, ValueError):
        return None
