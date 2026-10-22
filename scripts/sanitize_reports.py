"""Sanitise every committed verification artifact in place.

    python scripts/sanitize_reports.py [--check]

--check exits 1 when something WOULD change (useful in CI / pre-commit).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from report_util import sanitize_file  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TARGETS = sorted((ROOT / "docs" / "verification").glob("*.json"))


def main() -> int:
    check = "--check" in sys.argv
    dirty = []
    for path in TARGETS:
        if check:
            import json

            from report_util import scrub

            original = json.loads(path.read_text(encoding="utf-8"))
            if scrub(original) != original:
                dirty.append(path)
        elif sanitize_file(path):
            print(f"sanitised {path.relative_to(ROOT)}")
    if check:
        for p in dirty:
            print(f"NOT SANITISED: {p.relative_to(ROOT)}")
        print("ok" if not dirty else f"{len(dirty)} file(s) leak identifiers")
        return 0 if not dirty else 1
    print(f"checked {len(TARGETS)} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
