"""Build a human-readable verification report (Markdown + console summary)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# Per-entry outcome statuses.
STATUS_UPDATED = "updated"
STATUS_VERIFIED = "verified"        # found, matched, no changes needed
STATUS_NOT_FOUND = "not_found"      # no Scholar result / no BibTeX export
STATUS_MISMATCH = "mismatch"        # found something but not the same paper
STATUS_ERROR = "error"


@dataclass
class EntryResult:
    entry_id: str
    original_title: str
    status: str
    matched_title: Optional[str] = None
    similarity: float = 0.0
    method: str = ""
    changed_fields: list = field(default_factory=list)
    detail: str = ""


def summarize(results: list) -> dict:
    counts = {
        STATUS_UPDATED: 0,
        STATUS_VERIFIED: 0,
        STATUS_NOT_FOUND: 0,
        STATUS_MISMATCH: 0,
        STATUS_ERROR: 0,
    }
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return counts


def print_summary(results: list) -> None:
    counts = summarize(results)
    total = len(results)
    print("\n" + "=" * 60)
    print(f"Citation verification complete: {total} entries processed")
    print("-" * 60)
    print(f"  updated   : {counts[STATUS_UPDATED]}")
    print(f"  verified  : {counts[STATUS_VERIFIED]} (already correct)")
    print(f"  mismatch  : {counts[STATUS_MISMATCH]} (different paper found)")
    print(f"  not found : {counts[STATUS_NOT_FOUND]}")
    print(f"  errors    : {counts[STATUS_ERROR]}")
    print("=" * 60)
    if counts[STATUS_UPDATED]:
        print("Updated entries:")
        for r in results:
            if r.status == STATUS_UPDATED:
                print(f"  - {r.entry_id}: changed {', '.join(r.changed_fields)}")


def write_markdown(results: list, path: str, input_path: str, output_path: str) -> None:
    counts = summarize(results)
    total = len(results)
    lines = []
    lines.append("# Citation Verification Report\n")
    lines.append(f"- **Input:** `{input_path}`")
    lines.append(f"- **Verified output:** `{output_path}`")
    lines.append(f"- **Entries processed:** {total}\n")

    lines.append("## Summary\n")
    lines.append("| Status | Count |")
    lines.append("| --- | --- |")
    lines.append(f"| Updated | {counts[STATUS_UPDATED]} |")
    lines.append(f"| Verified (already correct) | {counts[STATUS_VERIFIED]} |")
    lines.append(f"| Mismatch (different paper) | {counts[STATUS_MISMATCH]} |")
    lines.append(f"| Not found | {counts[STATUS_NOT_FOUND]} |")
    lines.append(f"| Errors | {counts[STATUS_ERROR]} |\n")

    lines.append("## Details\n")
    lines.append("| Key | Status | Similarity | Method | Changed fields | Detail |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for r in results:
        changed = ", ".join(r.changed_fields) if r.changed_fields else "-"
        detail = (r.detail or r.matched_title or "").replace("|", "\\|")
        lines.append(
            f"| `{r.entry_id}` | {r.status} | {r.similarity:.2f} | "
            f"{r.method or '-'} | {changed} | {detail} |"
        )

    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
