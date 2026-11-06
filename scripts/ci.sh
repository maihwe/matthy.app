#!/usr/bin/env bash
#
# CI gates, runnable locally — 11 §6.
#
# 11 §3.2 is honest that with one builder there is no second reviewer, and that
# CI is therefore not a safety net over review but the review itself. This
# script is that review, runnable before a commit rather than after a push.
#
# The GitHub Actions workflow (.github/workflows/ci.yml) runs the same gates.
# Where the two could drift, this script is the source of truth for order and
# flags, and the workflow mirrors it.
#
# Usage:
#     scripts/ci.sh              # all gates
#     scripts/ci.sh lint types   # named gates only
#     scripts/ci.sh --list       # show gate names

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

GATES=(format lint types contracts migrations tests coverage secrets deps openapi)

if [[ "${1:-}" == "--list" ]]; then
    printf '%s\n' "${GATES[@]}"
    exit 0
fi

if [[ $# -gt 0 ]]; then
    selected=("$@")
else
    selected=("${GATES[@]}")
fi

# 12 §4 / SEC-SEC-005: gate 8 needs these to be *absent*, so they are never
# exported by this script. A gate runner that provides secrets is a gate runner
# that cannot detect a leaked one.

failures=()

run_gate() {
    local name="$1"
    shift
    printf '\n\033[1m── %s\033[0m\n' "${name}"
    if "$@"; then
        printf '\033[32m   PASS\033[0m\n' "${name}"
    else
        printf '\033[31m   FAIL\033[0m\n' "${name}"
        failures+=("${name}")
    fi
}

gate_format()   { ruff format --check .; }
gate_lint()     { ruff check .; }

# 11 §6: strict on the financial modules. A `float` where an `int` was meant is
# the money bug this project most needs not to have.
gate_types()    { mypy .; }

# 02 §5.4: the layer rules and the ledger inversion, enforced mechanically.
gate_contracts() { lint-imports; }

# Gate 5 — a model change with no migration.
gate_migrations() {
    python manage.py makemigrations --check --dry-run \
        --settings=config.settings.test
}

gate_tests() {
    # Suites 6a–6e of 13 §10 run as one invocation: every test carries its suite
    # marker, but no `-m` filter is applied here, so this runs the whole tree.
    # (The markers are not decorative — they are what lets a developer run one
    # suite locally — but nothing selects on them in CI.) 6b and 6d need real
    # PostgreSQL, which this script assumes is up.
    pytest -p no:randomly --cov --cov-report=xml --cov-report=term-missing
}

gate_coverage() {
    # 13 §9 specifies four per-scope floors, not one global floor. coverage.py
    # has a single `fail_under`, and 80 satisfies none of them — it would let
    # ledger/ sit at 81%, where an uncovered branch is an unposted transaction.
    coverage json -o coverage.json || return
    python scripts/check_coverage.py
}

# Gate 8 — no secret in source, ever (SEC-SEC-001).
#
# No baseline is committed, and that is deliberate. A detect-secrets baseline
# is a list of findings marked "known and accepted", and one committed before
# anyone has read it is how a real secret acquires the label "known". If false
# positives become a problem, the baseline is generated and reviewed in the
# same commit that adds it — not beforehand.
gate_secrets() {
    local report status
    report="$(mktemp)"

    detect-secrets scan --no-verify > "${report}"

    python - "${report}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)

found = 0
for path, findings in payload.get("results", {}).items():
    for finding in findings:
        found += 1
        print(
            f"  {path}:{finding.get('line_number')} {finding.get('type')}",
            file=sys.stderr,
        )

if found:
    print(f"\nerror: {found} potential secret(s) found.", file=sys.stderr)
    print(
        "A secret that reaches a commit is in the history permanently; "
        "rotating it is the only remedy (SEC-SEC-001).",
        file=sys.stderr,
    )
    sys.exit(1)

print("no secrets detected")
PY
    status=$?
    rm -f "${report}"
    return "${status}"
}

# 11 §6: fails on high severity (S14).
gate_deps()     { pip-audit --strict --vulnerability-service osv; }

# 11 §6, 04 §15: an undocumented endpoint change fails.
gate_openapi() {
    python manage.py spectacular \
        --settings=config.settings.test \
        --file /tmp/openapi.yaml \
        --validate
    if [[ -f docs/openapi.yaml ]]; then
        diff -u docs/openapi.yaml /tmp/openapi.yaml
    else
        printf '   note: docs/openapi.yaml does not exist yet; skipping the diff.\n'
        printf '   It is committed by the slice that adds the first endpoint.\n'
    fi
}

for gate in "${selected[@]}"; do
    case "${gate}" in
        format)     run_gate format     gate_format ;;
        lint)       run_gate lint       gate_lint ;;
        types)      run_gate types      gate_types ;;
        contracts)  run_gate contracts  gate_contracts ;;
        migrations) run_gate migrations gate_migrations ;;
        tests)      run_gate tests      gate_tests ;;
        coverage)   run_gate coverage   gate_coverage ;;
        secrets)    run_gate secrets    gate_secrets ;;
        deps)       run_gate deps       gate_deps ;;
        openapi)    run_gate openapi    gate_openapi ;;
        *)
            printf 'unknown gate: %s\n' "${gate}" >&2
            printf 'available: %s\n' "${GATES[*]}" >&2
            exit 2
            ;;
    esac
done

printf '\n'
if [[ ${#failures[@]} -eq 0 ]]; then
    printf '\033[32mAll %d gates passed.\033[0m\n' "${#selected[@]}"
    exit 0
fi

printf '\033[31m%d gate(s) failed:\033[0m\n' "${#failures[@]}"
printf '  - %s\n' "${failures[@]}"
printf '\nPer 13 §1, a PR that cannot pass CI has not been reviewed.\n'
exit 1
