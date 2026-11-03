#!/usr/bin/env python3
"""Check that README.en.md and README.fr.md keep the same structure.

The headings are translated, so we compare cheap structural counters that must
be identical in both languages (level-2 headings, code blocks, collapsible
sections, table rows, task-list items, alerts). Exit code 1 on any mismatch.

Usage: python scripts/check_readme_parity.py [repository-root]
"""
import re
import sys
from pathlib import Path

PAIR = ("README.en.md", "README.fr.md")


def metrics(text: str) -> dict[str, int]:
    """Return the structural counters of one README."""
    return {
        "level-2 headings": len(re.findall(r"^## ", text, re.M)),
        "fenced code blocks": len(re.findall(r"^```", text, re.M)) // 2,
        "<details> sections": text.count("<details>"),
        "table rows": len(re.findall(r"^\|", text, re.M)),
        "task-list items": len(re.findall(r"^- \[[ x]\]", text, re.M)),
        "alerts": len(re.findall(r"^> \[!", text, re.M)),
    }


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    found = [root / name for name in PAIR]
    for path in found:
        if not path.is_file():
            print(f"missing file: {path}")
            return 1
    en, fr = (metrics(p.read_text("utf-8")) for p in found)
    diffs = [f"  {key}: en={en[key]} fr={fr[key]}" for key in en if en[key] != fr[key]]
    if diffs:
        print("README.en.md and README.fr.md differ structurally:")
        print("\n".join(diffs))
        return 1
    print("README.en.md and README.fr.md are structurally identical.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
