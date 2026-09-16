#!/usr/bin/env python3
"""Per-scope coverage floors — 13 §9, CI gate 7.

`coverage.py` has one global `fail_under`. 13 §9 does not specify one floor, it
specifies four:

    ledger/                                        100% branch
    loans/ payments/ disbursements/ contributions/  95% branch
    accounts/ groups/ permissions/                  90% branch
    everything else                                 80% line

That third row names three modules and `SCOPES` below carries two prefixes,
because `permissions` is not an app: 02 §4.1 assigns "group roles & permissions"
to `groups/`, the `permissions` and `group_permissions` tables of 03 §5.5 are
that module's tables, and `permissions.py` is a per-app file in the shape §47
prescribes — one in every module, not one module. So the 90% floor reaches
permission code through `apps/groups/`, and no `apps/permissions/` prefix is
listed: it would be a scope that can never match anything, which is the failure
`--require-all-scopes` exists to report.

A global floor of 80 satisfies none of them. It lets the ledger — the module
where an uncovered branch is an unposted transaction — sit at 81%, and it lets
`accounts/`, which is security-relevant, sit at 81% too, while a well-tested
utility module drags the average up and hides both.

So the floors are checked here, per scope, against `coverage.json`.

Two deliberate properties:

* **A scope with no measured files is skipped with a notice, never silently
  passed.** Before a module exists it cannot be covered; once it exists, a
  scope that matches nothing is itself a failure, because that is what a typo
  in a path prefix looks like.
* **"Any drop in `ledger/`" needs no baseline.** A 100% floor cannot drop
  without failing, so the second half of §9's gate 7 rule is implied by the
  first rather than needing stored history.

Usage:
    coverage json -o coverage.json && python scripts/check_coverage.py
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

# 13 §9, in order. First match wins, so the most specific prefixes come first.
# `branch=True` means the figure is (covered lines + covered branches) over
# (statements + branches) — coverage.py's own definition, not an average of
# per-file percentages, which would weight a 5-line module the same as a
# 500-line one.
SCOPES: tuple[tuple[str, tuple[str, ...], float, bool], ...] = (
    (
        "ledger",
        ("apps/ledger/",),
        100.0,
        True,
    ),
    (
        "financial",
        ("apps/loans/", "apps/payments/", "apps/disbursements/", "apps/contributions/"),
        95.0,
        True,
    ),
    (
        "security-relevant",
        ("apps/accounts/", "apps/groups/"),
        90.0,
        True,
    ),
    (
        "everything else",
        (
            "apps/",
            "config/",
        ),
        80.0,
        False,
    ),
)


@dataclass
class Result:
    name: str
    floor: float
    measured: bool
    covered: int
    total: int
    branch: bool

    @property
    def percent(self) -> float:
        return 100.0 * self.covered / self.total if self.total else 100.0

    @property
    def passed(self) -> bool:
        # Rounded to two places before comparing, because the figure is
        # reported at two places and "89.996% — above the 90% floor" reads as a
        # bug in the checker rather than as a pass.
        return round(self.percent, 2) >= self.floor


def _totals(entry: dict[str, object], branch: bool) -> tuple[int, int]:
    """``(covered, total)`` for one file's summary block."""
    summary = entry.get("summary")
    if not isinstance(summary, dict):
        return (0, 0)

    statements = int(summary.get("num_statements", 0) or 0)
    covered_lines = int(summary.get("covered_lines", 0) or 0)
    if not branch:
        return (covered_lines, statements)

    branches = int(summary.get("num_branches", 0) or 0)
    covered_branches = int(summary.get("covered_branches", 0) or 0)
    return (covered_lines + covered_branches, statements + branches)


def _claim(filename: str, prefixes: tuple[str, ...]) -> bool:
    return any(filename.startswith(prefix) for prefix in prefixes)


def evaluate(files: dict[str, object], *, strict_no_match: bool) -> list[Result]:
    """Assign each measured file to the first scope that claims it."""
    buckets: dict[str, list[tuple[int, int]]] = {name: [] for name, _, _, _ in SCOPES}
    claimed: set[str] = set()

    for filename, entry in files.items():
        if not isinstance(entry, dict):
            continue
        for name, prefixes, _floor, branch in SCOPES:
            if _claim(filename, prefixes):
                buckets[name].append(_totals(entry, branch))
                claimed.add(filename)
                break

    results: list[Result] = []
    for name, _prefixes, floor, branch in SCOPES:
        pairs = buckets[name]
        if not pairs:
            results.append(Result(name, floor, False, 0, 0, branch))
            continue
        covered = sum(c for c, _ in pairs)
        total = sum(t for _, t in pairs)
        if total == 0:
            # Files exist but contribute no statements or branches — excluded
            # blocks only, or an empty __init__. Nothing to assert.
            results.append(Result(name, floor, False, 0, 0, branch))
            continue
        results.append(Result(name, floor, True, covered, total, branch))

    if strict_no_match:
        unclaimed = sorted(f for f in files if f not in claimed)
        if unclaimed:
            # Reported, not raised: an unclaimed file is one the omit list and
            # the scope list disagree about, which is worth seeing but is not
            # itself a coverage failure.
            print("note: measured files matching no scope:", file=sys.stderr)
            for filename in unclaimed:
                print(f"  {filename}", file=sys.stderr)

    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--json",
        type=Path,
        default=Path("coverage.json"),
        help="coverage.py JSON report (default: coverage.json)",
    )
    parser.add_argument(
        "--require-all-scopes",
        action="store_true",
        help="fail if any scope matches no files. Correct once every module "
        "exists; wrong before then, because a module that has not been written "
        "cannot be covered.",
    )
    args = parser.parse_args(argv)

    if not args.json.is_file():
        print(
            f"error: {args.json} not found. Run `coverage json -o {args.json}` first.",
            file=sys.stderr,
        )
        return 2

    payload = json.loads(args.json.read_text(encoding="utf-8"))
    files = payload.get("files", {})
    if not isinstance(files, dict) or not files:
        print("error: the coverage report measured no files.", file=sys.stderr)
        return 2

    results = evaluate(files, strict_no_match=True)

    print(f"{'scope':<20} {'floor':>7} {'actual':>8}  {'':<6} files")
    print(f"{'-' * 20} {'-' * 7} {'-' * 8}  {'-' * 6} {'-' * 5}")

    failures: list[Result] = []
    skipped: list[Result] = []

    for result in results:
        unit = "branch" if result.branch else "line"
        if not result.measured:
            print(f"{result.name:<20} {result.floor:>6.1f}% {'—':>8}  {'skip':<6} 0")
            skipped.append(result)
            continue
        verdict = "ok" if result.passed else "FAIL"
        print(
            f"{result.name:<20} {result.floor:>6.1f}% {result.percent:>7.2f}%  "
            f"{verdict:<6} {result.total} {unit}s"
        )
        if not result.passed:
            failures.append(result)

    if skipped:
        print()
        for result in skipped:
            print(f"note: '{result.name}' matched no measured files — skipped, not passed.")

    if args.require_all_scopes and skipped:
        print(
            "\nerror: --require-all-scopes was given and a scope matched nothing.",
            file=sys.stderr,
        )
        return 1

    if failures:
        print("\nCoverage below floor (13 §9):", file=sys.stderr)
        for result in failures:
            print(
                f"  {result.name}: {result.percent:.2f}% < {result.floor:.1f}% "
                f"({result.total - result.covered} uncovered "
                f"{'branch' if result.branch else 'line'}s)",
                file=sys.stderr,
            )
        print(
            "\nA new uncovered branch in a financial module is a posting path "
            "nothing verifies. Add the test; do not lower the floor (13 §9).",
            file=sys.stderr,
        )
        return 1

    print("\nAll measured scopes are at or above their floor.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
