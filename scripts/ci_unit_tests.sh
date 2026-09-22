#!/usr/bin/env bash
#
# Gate the unit test suite by running each test file in its OWN process.
#
# Why per-file: several test modules install module-level ``sys.modules`` stubs
# for optional platform deps (slack_sdk, aiohttp, modules.agents.*, core.*) that
# never tear down. Collecting the whole ``tests/`` tree in a single process
# therefore fails at import time as those stubs shadow real packages for later
# modules. Running each file in its own interpreter sidesteps that cross-file
# pollution. Making the stubs fixture-scoped so a single ``pytest tests/`` works
# is tracked separately; until then this is how the unit suite is CI-gated.
#
# CI may pass a shard index and total shard count to split the file list across
# multiple runners while preserving the one-process-per-file isolation. Shards
# use the checked-in per-file timing snapshot when available and fall back to a
# deterministic file-size/test-count estimate for new or missing files.
#
# Each file gets its own watchdog budget from that same snapshot: a multiple of
# what the file itself has recorded, floored at CI_TEST_FILE_TIMEOUT_SECONDS. A
# single fixed budget cannot separate "hung" from "slow" across a suite whose
# files span three orders of magnitude -- it is generous for a 1s file and thin
# for a 74s one, so a uniformly slow runner kills the slowest file first and
# names it as the culprit. Files with no recorded duration keep the floor.
#
# Excludes ``tests/e2e`` (Docker) and the ``integration`` marker (Docker +
# platform tokens) — those run in dedicated jobs. ``-p no:randomly`` keeps a
# deterministic order if pytest-randomly happens to be installed (no-op when it
# is not), and ``-o addopts=""`` ignores any ambient addopts.
set -uo pipefail

SHARD_INDEX="${1:-0}"
SHARD_TOTAL="${2:-1}"

case "$SHARD_INDEX" in
  ''|*[!0-9]*)
    echo "Shard index must be a non-negative integer, got: $SHARD_INDEX" >&2
    exit 2
    ;;
esac

case "$SHARD_TOTAL" in
  ''|*[!0-9]*)
    echo "Shard total must be a positive integer, got: $SHARD_TOTAL" >&2
    exit 2
    ;;
esac

if [ "$SHARD_TOTAL" -lt 1 ]; then
  echo "Shard total must be at least 1, got: $SHARD_TOTAL" >&2
  exit 2
fi

if [ "$SHARD_INDEX" -ge "$SHARD_TOTAL" ]; then
  echo "Shard index must be less than shard total, got: $SHARD_INDEX >= $SHARD_TOTAL" >&2
  exit 2
fi

PYTHON_BIN="${PYTHON:-}"
if [ -z "$PYTHON_BIN" ]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
  else
    echo "Unable to find python3 or python on PATH." >&2
    exit 127
  fi
fi

# The floor under every per-file budget, and the whole budget for any file the
# timing snapshot does not know about.
FILE_TIMEOUT="${CI_TEST_FILE_TIMEOUT_SECONDS:-300}"
case "$FILE_TIMEOUT" in
  ''|*[!0-9]*)
    echo "CI_TEST_FILE_TIMEOUT_SECONDS must be a positive integer, got: $FILE_TIMEOUT" >&2
    exit 2
    ;;
esac
if ! [ "$FILE_TIMEOUT" -gt 0 ]; then
  echo "CI_TEST_FILE_TIMEOUT_SECONDS must be a positive integer, got: $FILE_TIMEOUT" >&2
  exit 2
fi

# Keep one interpreter per file. The watchdog also covers collection and
# interpreter shutdown; pytest's per-test faulthandler timer would replace it.
PYTEST=("$PYTHON_BIN" -u -c '
import faulthandler, os, sys, time
started_at = time.perf_counter()
# Retain the original output even while pytest redirects descriptor 2.
diagnostics = os.fdopen(os.dup(sys.stderr.fileno()), "w")
faulthandler.enable(file=diagnostics)
faulthandler.dump_traceback_later(int(sys.argv.pop(1)), file=diagnostics, exit=True)
import pytest
from scripts.ci_pytest_metrics import FileMetrics
metrics = FileMetrics(sys.argv[1], started_at)
exit_code = pytest.main(sys.argv[1:], plugins=[metrics])
metrics.emit(diagnostics, int(exit_code))
sys.exit(exit_code)
')

# Emits "<path>\t<budget seconds>\t<recorded seconds, blank if unknown>".
select_unit_test_files() {
  "$PYTHON_BIN" scripts/ci_unit_test_shards.py \
    --budget-floor="$FILE_TIMEOUT" "$SHARD_INDEX" "$SHARD_TOTAL"
}

failed=""
empty=""
selected=0
discovered=0
while IFS=$'\t' read -r f budget baseline; do
  [ -n "$f" ] || continue
  case "$budget" in ''|*[!0-9]*) budget="$FILE_TIMEOUT" ;; esac
  discovered=$((discovered + 1))
  selected=$((selected + 1))
  started_at=$(date +%s)
  if [ -n "$baseline" ]; then
    echo "Starting $f (timeout ${budget}s, budgeted from its recorded ${baseline}s)."
  else
    echo "Starting $f (timeout ${budget}s)."
  fi
  "${PYTEST[@]}" "$budget" "$f" -m "not integration" -p no:randomly -p no:faulthandler -o addopts="" -v
  rc=$?
  finished_at=$(date +%s)
  elapsed=$((finished_at - started_at))
  echo "Finished $f in ${elapsed}s with exit code $rc."
  if [ "$rc" -ne 0 ] && [ "$elapsed" -ge "$budget" ]; then
    # Say what the dump above is and is not. faulthandler prints whichever frame
    # the main thread occupied when the budget expired; in a file whose cost is
    # spread over many tests that is usually an ordinary cheap frame, and reading
    # it as the stall site sends the next reader after an innocent test.
    echo "  ^ $f hit its ${budget}s watchdog."
    if [ -n "$baseline" ]; then
      echo "    That budget is a multiple of this file's own recorded ${baseline}s, so being the slowest file is not enough to reach it."
    else
      echo "    No recorded duration for this file, so it ran on the ${FILE_TIMEOUT}s floor; refresh scripts/ci_unit_test_timings.json to give it a budget of its own."
    fi
    echo "    The traceback above is where the main thread stood when the budget expired, not necessarily where it stalled."
    echo "    Before blaming the named test, compare the 'Finished ... in Ns' lines in this shard against scripts/ci_unit_test_timings.json: if they are slow across the board, the runner was slow, not this file."
  fi
  if [ "$rc" -eq 0 ]; then
    :
  elif [ "$rc" -eq 5 ]; then
    # exit 5 = no tests collected (e.g. a file that is entirely integration-marked)
    empty="$empty $f"
  else
    failed="$failed $f"
  fi
done < <(select_unit_test_files)

discovered=$(find tests -name 'test_*.py' -not -path 'tests/e2e/*' | sort | wc -l | tr -d ' ')

echo "========================================"
echo "Discovered ${discovered} unit test file(s)."
echo "Ran ${selected} unit test file(s), one process each, for shard ${SHARD_INDEX}/${SHARD_TOTAL}."
if [ -n "$empty" ]; then
  echo "No unit tests collected (skipped):"
  for f in $empty; do echo "  $f"; done
fi
if [ -n "$failed" ]; then
  echo "FAILED files:"
  for f in $failed; do echo "  $f"; done
  exit 1
fi
echo "All unit test files passed."
