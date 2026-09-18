#!/bin/sh
# Tests the failure semantics of the stream-metrics task (issue #73):
# `give_up` must ALWAYS record a status on the PVC, and must fail or not fail
# according to requireMetrics.
#
# Run with: sh tektonc-data-collection/tests/test_stream_metrics_failure_semantics.sh
#
# The function under test is EXTRACTED from tekton/tasks/stream-metrics.yaml
# rather than copied here, so this cannot pass against a stale duplicate of
# logic that has since changed in the task. If the extraction finds nothing the
# test fails loudly instead of vacuously passing.

TASK_YAML="$(dirname "$0")/../tekton/tasks/stream-metrics.yaml"
TMPDIR_TEST=$(mktemp -d)
trap 'rm -rf "${TMPDIR_TEST}"' EXIT

PASS=true

fail_msg() {
  echo "FAIL: $1"
  PASS=false
}

assert_eq() {
  if [ "$1" = "$2" ]; then
    echo "PASS: $3"
  else
    fail_msg "$3 (got '$1', expected '$2')"
  fi
}

assert_contains() {
  if grep -q "$2" "$1" 2>/dev/null; then
    echo "PASS: $3"
  else
    fail_msg "$3 — '$2' not found in $1 (content: '$(cat "$1" 2>/dev/null)')"
  fi
}

# ── extract wait_for_sentinel() + give_up() from the task, dedented ──────
FUNCS="${TMPDIR_TEST}/funcs.sh"
{
  sed -n '/^        wait_for_sentinel() {/,/^        }/p' "${TASK_YAML}"
  sed -n '/^        give_up() {/,/^        }/p' "${TASK_YAML}"
} | sed 's/^        //' > "${FUNCS}"

for fn in wait_for_sentinel give_up; do
  if ! grep -q "^${fn}() {" "${FUNCS}"; then
    echo "FAIL: could not extract ${fn}() from ${TASK_YAML} — was it renamed?"
    echo "      This test asserts the task's real logic; fix the extraction."
    exit 1
  fi
done
echo "PASS: extracted wait_for_sentinel() + give_up() ($(wc -l < "${FUNCS}" | tr -d ' ') lines)"

# ── harness: run give_up with a given requireMetrics ─────────────────────
# `local` is a bashism in the extracted function, so run under bash, which is
# also what the task's own shebang uses. The sentinel is pre-created by default
# so the hard-fail path's wait returns immediately; the deferral itself is
# tested separately below.
run_give_up() {
  _require="$1"
  _dir="${TMPDIR_TEST}/$2"
  _sentinel_exists="${3:-yes}"
  mkdir -p "${_dir}"
  [ "${_sentinel_exists}" = yes ] && touch "${_dir}/metrics_stream_done"
  cat > "${TMPDIR_TEST}/harness_$2.sh" <<HARNESS
REQUIRE_METRICS="${_require}"
STATUS_FILE="${_dir}/collection_status"
SENTINEL="${_dir}/metrics_stream_done"
. "${FUNCS}"
give_up "some phase" "some detail"
echo "UNREACHABLE"
HARNESS
  bash "${TMPDIR_TEST}/harness_$2.sh" > "${_dir}/stdout" 2>&1
  echo $?
}

# ── requireMetrics=true → task fails, status recorded ────────────────────
rc=$(run_give_up true hard)
assert_eq "${rc}" "1" "requireMetrics=true exits non-zero"
assert_contains "${TMPDIR_TEST}/hard/collection_status" "^failed some phase: some detail$" \
  "requireMetrics=true records 'failed <phase>: <detail>'"
if grep -q "UNREACHABLE" "${TMPDIR_TEST}/hard/stdout"; then
  fail_msg "give_up returned instead of exiting (requireMetrics=true)"
else
  echo "PASS: give_up exits rather than returning (requireMetrics=true)"
fi

# ── requireMetrics=false → task passes, status STILL recorded ────────────
# This is the half that makes the soft-fail survivable: the old code exited 0
# and wrote nothing, so a lost run was indistinguishable from a good one.
rc=$(run_give_up false soft)
assert_eq "${rc}" "0" "requireMetrics=false exits zero"
assert_contains "${TMPDIR_TEST}/soft/collection_status" "^failed some phase: some detail$" \
  "requireMetrics=false STILL records the failure on the PVC"
if grep -q "UNREACHABLE" "${TMPDIR_TEST}/soft/stdout"; then
  fail_msg "give_up returned instead of exiting (requireMetrics=false)"
else
  echo "PASS: give_up exits rather than returning (requireMetrics=false)"
fi

# ── only the exact string "false" may opt out ────────────────────────────
# Fail-safe polarity: a mistyped or empty param must NOT silently restore the
# old silent-loss behaviour, so anything that is not exactly "false" fails.
for bad in "" "True" "yes" "0" "FALSE"; do
  label=$(echo "${bad}" | tr -c 'A-Za-z0-9' '_')
  rc=$(run_give_up "${bad}" "bad${label}")
  assert_eq "${rc}" "1" "requireMetrics='${bad}' fails (only exact 'false' opts out)"
done

# ── the hard-fail path must DEFER until the sentinel arrives ─────────────
# The reason this matters: stream-metrics runs in PARALLEL with the workload,
# and collect-results is an ordinary pipeline task, not a `finally` one. Exiting
# non-zero early stops Tekton scheduling collect-results, so the workload's
# results are never collected and stream-epp-logs / stream-gpu-stats poll for
# sentinels that never arrive. Failing over lost metrics must not cost the whole
# run's data.
DEFER_DIR="${TMPDIR_TEST}/defer"
mkdir -p "${DEFER_DIR}"
cat > "${TMPDIR_TEST}/harness_defer.sh" <<HARNESS
REQUIRE_METRICS="true"
STATUS_FILE="${DEFER_DIR}/collection_status"
SENTINEL="${DEFER_DIR}/metrics_stream_done"
. "${FUNCS}"
give_up "some phase" "some detail"
HARNESS
bash "${TMPDIR_TEST}/harness_defer.sh" > "${DEFER_DIR}/stdout" 2>&1 &
DEFER_PID=$!

sleep 2
if kill -0 "${DEFER_PID}" 2>/dev/null; then
  echo "PASS: hard-fail waits for the sentinel instead of exiting immediately"
else
  fail_msg "hard-fail exited before the sentinel appeared — this would stop Tekton scheduling collect-results"
fi

# The status marker must already be on the PVC while it waits, so the failure
# is discoverable without waiting for the task to end.
assert_contains "${DEFER_DIR}/collection_status" "^failed some phase" \
  "status marker is written immediately, before the wait"

# Now let it finish and confirm it still fails.
touch "${DEFER_DIR}/metrics_stream_done"
wait "${DEFER_PID}"; defer_rc=$?
assert_eq "${defer_rc}" "1" "hard-fail exits 1 once the sentinel arrives"

# ── every abort in the scraper step must route through give_up ────────────
# The pre-#73 code aborted with four inline `exit 0`s. Assert that the scraper
# step now contains NO `exit 0` outside give_up itself, which is the invariant
# that keeps failures recorded. Scoped to that step, because provide-kubectl
# legitimately ends in `exit 0` (see below).
# Comment lines are stripped first: the script legitimately *describes* the old
# `exit 0` behaviour in a comment, and matching that would be a false positive.
SCRAPER="${TMPDIR_TEST}/scraper_region.txt"
sed -n '/^    - name: stream-metrics$/,$p' "${TASK_YAML}" \
  | sed '/^        give_up() {/,/^        }/d' \
  | grep -vE '^\s*#' > "${SCRAPER}"
STRAY=$(grep -c 'exit 0' "${SCRAPER}" || true)
assert_eq "${STRAY}" "0" "scraper step has no 'exit 0' outside give_up (comments excluded)"

# ── provide-kubectl must NOT be able to fail the TaskRun ─────────────────
# give_up and its deferred wait live in the scraper step, so a non-zero exit
# from the staging step bypasses all of it: Tekton stops scheduling
# collect-results and nothing is recorded, because RESULTS_DIR/metrics does not
# exist yet. Staging is therefore best-effort and adjudicated later.
STAGER="${TMPDIR_TEST}/stager_region.txt"
sed -n '/^    - name: provide-kubectl$/,/^    - name: stream-metrics$/p' \
  "${TASK_YAML}" > "${STAGER}"
if grep -qE '^\s+set -e|^\s+set -eu|^\s+set -ue' "${STAGER}"; then
  fail_msg "provide-kubectl uses 'set -e' — a staging failure would fail the TaskRun and bypass give_up"
else
  echo "PASS: provide-kubectl does not use 'set -e'"
fi
if grep -qE '^\s+exit 0\s*$' "${STAGER}"; then
  echo "PASS: provide-kubectl ends with an explicit exit 0"
else
  fail_msg "provide-kubectl has no explicit 'exit 0' — a failing last command would fail the TaskRun"
fi

# ── phase 6: zero scrapes fails, non-zero scrapes writes the ok marker ───
# Issue #73 asks for a marker distinguishing "collected" from "gave up". Only
# the "gave up" half was covered above; this covers "collected", and the gate
# that decides between them.
PHASE6="${TMPDIR_TEST}/phase6.sh"
sed -n '/# Phase 6 — final report + status marker/,$p' "${TASK_YAML}" \
  | sed 's/^        //' \
  | sed 's/\$(params\.resultsDir)/TESTRUN/g' > "${PHASE6}"

if ! grep -q 'n_raw=' "${PHASE6}"; then
  echo "FAIL: could not extract phase 6 from ${TASK_YAML} — did the banner change?"
  PASS=false
else
  run_phase6() {
    _raw_count="$1"
    _dir="${TMPDIR_TEST}/p6_$2"
    mkdir -p "${_dir}/metrics/raw" "${_dir}/metrics/processed"
    i=0
    while [ "${i}" -lt "${_raw_count}" ]; do
      touch "${_dir}/metrics/raw/pod${i}_123_metrics.log"
      i=$((i + 1))
    done
    touch "${_dir}/metrics/processed/metrics_summary.json"
    cat > "${TMPDIR_TEST}/h6_$2.sh" <<H6
REQUIRE_METRICS="true"
RESULTS_DIR="${_dir}"
STATUS_FILE="${_dir}/metrics/collection_status"
SENTINEL="${_dir}/metrics_stream_done"
LOG="${_dir}/metrics/metrics_collection.log"
touch "\${SENTINEL}"
. "${FUNCS}"
. "${PHASE6}"
H6
    bash "${TMPDIR_TEST}/h6_$2.sh" > "${_dir}/stdout" 2>&1
    echo $?
  }

  rc=$(run_phase6 0 zero)
  assert_eq "${rc}" "1" "phase 6 fails when 0 raw scrapes were produced"
  assert_contains "${TMPDIR_TEST}/p6_zero/metrics/collection_status" "^failed scrape" \
    "phase 6 records 'failed scrape' when nothing was collected"

  rc=$(run_phase6 3 three)
  assert_eq "${rc}" "0" "phase 6 succeeds when raw scrapes exist"
  assert_contains "${TMPDIR_TEST}/p6_three/metrics/collection_status" \
    "^ok raw=3 processed=1$" \
    "phase 6 records 'ok raw=3 processed=1' on success"
fi

if grep -q "dl.k8s.io/release/.*kubectl" "${TASK_YAML}"; then
  fail_msg "task still downloads kubectl from dl.k8s.io"
else
  echo "PASS: task no longer downloads kubectl"
fi

echo
if [ "${PASS}" = true ]; then
  echo "ALL TESTS PASSED"
  exit 0
fi
echo "SOME TESTS FAILED"
exit 1
