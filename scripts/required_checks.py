"""Print the checks a coding job's changes require, one command per line.

The coding session runs this with the files it changed, then runs each command
and fixes what fails before it finishes. The commands come from the same
`required_verification` AL/X applies afterwards, so the session meets the
checks while it can still correct them in place: on 2026-10-22 two coding jobs
were lost to wording tests the session never ran.

The git diff check is left out because a coding session cannot read git
metadata; AL/X runs it afterwards. Only AL/X's own run of these checks counts.

Usage: python scripts/required_checks.py <changed file> [<changed file> ...]
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alx.contracts.coding_verification import DIFF_CHECK, required_verification  # noqa: E402


def main(changed: list[str]) -> int:
    if not changed:
        print("usage: python scripts/required_checks.py <changed file> ...", file=sys.stderr)
        return 2
    policy = required_verification(changed, ROOT)
    for check in policy.checks:
        if check.kind == "command" and check.argv and check.argv != DIFF_CHECK:
            print(shlex.join(check.argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
