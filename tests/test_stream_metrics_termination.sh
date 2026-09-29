#!/bin/sh
# Asserts that all three streaming tasks terminate the same way.
#
# stream-epp-logs, stream-gpu-stats and stream-metrics each poll for a sentinel
# that collect-results writes when the workload finishes. Two properties are
# required of each:
#
#   1. The sentinel is cleared before the task's own bootstrap, not after.
#      collect-results runs in parallel with the streamers, so a workload
#      finishing during bootstrap has its sentinel written and then deleted by a
#      later clear, with nothing left to re-create it — and the poll loop runs to
#      the TaskRun timeout, failing the PipelineRun and holding a namespace slot.
#
#   2. The poll loop has a second exit for "the workload disappeared without the
#      sentinel reaching me": no EPP pods remain, or no vLLM pods remain. It must
#      be reachable — a check against a process that never exits does not count.
#
# Cluster-free: property 1 is asserted against the real task YAML, property 2
# against a drop-in copy of the poll loop with stubbed probes.
#
# Run with: sh tektonc-data-collection/tests/test_stream_metrics_termination.sh

HERE=$(dirname "$0")
TASKS="${HERE}/../tekton/tasks"
EXTRACT="${HERE}/lib/extract_step.py"

PYTHON=python3
for cand in "${HERE}/../../.venv/bin/python" "${HERE}/../.venv/bin/python"; do
  [ -x "${cand}" ] && PYTHON="${cand}" && break
done

PASS=true
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; PASS=false; }

WORK=$(mktemp -d)
trap 'rm -rf "${WORK}"' EXIT

# ────────────────────────────────────────────────────────────
# Property 1 — sentinel cleared before bootstrap (stream-metrics)
# ────────────────────────────────────────────────────────────
# Compares the line number of the sentinel clear against the first command that
# can block on the network (apt-get or curl). The clear must come first: anything
# that can stall is long enough for collect-results to win the race.
"${PYTHON}" "${EXTRACT}" "${TASKS}/stream-metrics.yaml" stream-metrics \
  > "${WORK}/metrics.sh" 2>/dev/null
if [ ! -s "${WORK}/metrics.sh" ]; then
  fail "could not extract the stream-metrics step script"
else
  RM_LINE=$(grep -n 'rm -f "${SENTINEL}"' "${WORK}/metrics.sh" | head -1 | cut -d: -f1)
  BOOT_LINE=$(grep -nE '^\s*(apt-get|curl )' "${WORK}/metrics.sh" | head -1 | cut -d: -f1)
  if [ -z "${RM_LINE}" ]; then
    fail "stream-metrics does not clear the sentinel at all"
  elif [ -z "${BOOT_LINE}" ]; then
    fail "could not locate the bootstrap (apt-get/curl) in stream-metrics"
  elif [ "${RM_LINE}" -lt "${BOOT_LINE}" ]; then
    pass "stream-metrics clears the sentinel (line ${RM_LINE}) before bootstrap (line ${BOOT_LINE})"
  else
    fail "stream-metrics clears the sentinel at line ${RM_LINE}, AFTER bootstrap at line ${BOOT_LINE} — a workload finishing during bootstrap loses its sentinel"
  fi

  # The clear must exist at all. Removing it rather than ordering it correctly
  # trades the hang for a silent no-metrics run: nothing else clears a stale
  # sentinel, since prepare-results-dir only mkdir -p's and collect-results clears
  # in the same step that later writes it.
  if grep -q 'rm -f "${SENTINEL}"' "${WORK}/metrics.sh"; then
    pass "stream-metrics still clears a stale sentinel before watching"
  else
    fail "stream-metrics dropped the stale-sentinel clear"
  fi
fi

# ────────────────────────────────────────────────────────────
# Property 2 — every streamer has a live workload-disappeared exit
# ────────────────────────────────────────────────────────────
# Each streamer must break out of its poll loop when the thing it watches is
# gone, not only when the sentinel appears. Asserted as "the script probes for
# remaining pods after it starts streaming", which is what all three use.
check_pod_exit() {
  _task="$1"; _step="$2"; _needle="$3"
  "${PYTHON}" "${EXTRACT}" "${TASKS}/${_task}.yaml" "${_step}" \
    > "${WORK}/${_task}.sh" 2>/dev/null
  if [ ! -s "${WORK}/${_task}.sh" ]; then
    fail "could not extract ${_task} step ${_step}"
    return
  fi
  if grep -q "${_needle}" "${WORK}/${_task}.sh"; then
    pass "${_task} breaks when its pods are gone (${_needle})"
  else
    fail "${_task} has no workload-disappeared exit; a lost sentinel hangs it to the TaskRun timeout"
  fi
}

check_pod_exit stream-epp-logs  stream-epp-pod-logs "No running EPP pods remain"
check_pod_exit stream-gpu-stats stream-gpu-stats    "No running vLLM pods remain"
check_pod_exit stream-metrics   stream-metrics      "No running vLLM pods remain"

# ────────────────────────────────────────────────────────────
# Property 2b — the loop actually exits, not just mentions the string
# ────────────────────────────────────────────────────────────
# Drop-in copy of stream-metrics' Phase 4 loop. SCRAPER_PID is $$ (always
# alive, mirroring collect_metrics.sh's `while true`), so the only way out is
# the sentinel or the pod probe.
SENTINEL="${WORK}/metrics_stream_done"
SCRAPER_PID=$$
STUB_PODS="pod-a"

discover_vllm_pods() { echo "${STUB_PODS}"; }

poll_loop() {
  _iter=0
  while true; do
    _iter=$((_iter+1))
    if [ "${_iter}" -gt 50 ]; then echo "HUNG"; return; fi
    if [ -f "${SENTINEL}" ]; then echo "SENTINEL"; return; fi
    if ! kill -0 "${SCRAPER_PID}" 2>/dev/null; then echo "SCRAPER_DIED"; return; fi
    if [ -z "$(discover_vllm_pods)" ]; then echo "PODS_GONE"; return; fi
  done
}

# (a) sentinel present -> exits via the normal path
: > "${SENTINEL}"
[ "$(poll_loop)" = "SENTINEL" ] \
  && pass "loop exits on the sentinel" \
  || fail "loop did not exit on the sentinel"

# (b) sentinel lost (deleted after collect-results wrote it) but pods gone ->
#     exits instead of hanging. This is the race the reordering prevents and
#     this exit survives.
rm -f "${SENTINEL}"
STUB_PODS=""
[ "$(poll_loop)" = "PODS_GONE" ] \
  && pass "loop exits when the workload is gone and the sentinel was lost" \
  || fail "loop hung with no sentinel and no pods — the 3h-timeout case"

# (c) neither: still streaming, loop must NOT exit early
STUB_PODS="pod-a"
[ "$(poll_loop)" = "HUNG" ] \
  && pass "loop keeps polling while pods are alive and no sentinel exists" \
  || fail "loop exited early while the workload was still running"

${PASS} && { echo "ALL PASS"; exit 0; } || { echo "FAILURES"; exit 1; }
