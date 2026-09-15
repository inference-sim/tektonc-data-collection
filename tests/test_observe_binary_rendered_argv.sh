#!/bin/sh
# Tests that run-workload-blis-observe-binary consumes a pre-rendered argv
# (inference-sim/tektonc-data-collection#70):
#   - Part 1: structural — exactly 4 params, observeArgs required (no default),
#     the nine absorbed scalars gone, and no workload-kind branch left in the
#     run-observe step.
#   - Part 2: behavioral — the step's argv, against a FAKE blis that records one
#     argument per line. Recording per-line rather than with `echo "$@"` is the
#     point of this file: it pins word boundaries, which is the whole contract.
#     A trace-shaped and a synthetic-shaped observeArgs go through the SAME code
#     path, which is what "no kind branch" means operationally.
#   - Part 3: AC-4 — workloadSpec empty still writes /workspace/workload.yaml
#     empty, and run-observe never reads it.
#
# Not covered, by design: a `"` or `$` inside observeArgs. Tekton substitutes
# params textually, so such a value would break out of the OBSERVE_ARGS
# assignment. The param description forbids shell metacharacters; enforcing that
# belongs to the renderer (inference-sim/sim2real#900), not to this task.
#
# Run with: sh tektonc-data-collection/tests/test_observe_binary_rendered_argv.sh

PASS=true
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; PASS=false; }

HERE="$(dirname "$0")"
TASK="${HERE}/../tekton/tasks/run-workload-blis-observe-binary.yaml"
EXTRACT="${HERE}/lib/extract_step.py"

[ -f "${TASK}" ] || { echo "FAIL: cannot find ${TASK}"; exit 1; }

python3 -c 'import yaml' 2>/dev/null || {
  echo "SKIP: PyYAML not available (pip install -r tektonc/requirements.txt)"
  exit 0
}

WWS="$(python3 "${EXTRACT}" "${TASK}" write-workload-spec)" \
  || { echo "FAIL: no write-workload-spec step"; exit 1; }
RUN="$(python3 "${EXTRACT}" "${TASK}" run-observe)" \
  || { echo "FAIL: no run-observe step"; exit 1; }

# ────────────────────────────────────────────────────────────
# Part 1 — structural
# ────────────────────────────────────────────────────────────

# The param set is the deliverable: 12 -> 4. Asserted as an exact set, so a
# re-added scalar fails here rather than quietly growing the surface back.
if python3 - "${TASK}" <<'PY'
import sys, yaml
got = {p["name"] for p in yaml.safe_load(open(sys.argv[1]))["spec"]["params"]}
want = {"endpoint", "observeArgs", "workloadSpec", "resultsDir"}
if got == want:
    sys.exit(0)
print(f"extra={sorted(got - want)} missing={sorted(want - got)}", file=sys.stderr)
sys.exit(1)
PY
then pass "params are exactly {endpoint, observeArgs, workloadSpec, resultsDir}"
else fail "param set is not the 4 expected (see above)"
fi

# observeArgs MUST be required. A "" default would let a not-yet-updated
# Pipeline reach blis with no workload source instead of failing at PipelineRun
# creation with a Tekton param error.
if python3 - "${TASK}" <<'PY'
import sys, yaml
p = {x["name"]: x for x in yaml.safe_load(open(sys.argv[1]))["spec"]["params"]}
a = p.get("observeArgs")
sys.exit(0 if a and a.get("type") == "string" and "default" not in a else 1)
PY
then pass "observeArgs declared, string, NO default (required)"
else fail "observeArgs missing, mistyped, or has a default"
fi

# Each absorbed scalar was referenced exactly once, only to build the command.
for P in model maxConcurrency timeout warmupRequests prewarmDuration \
         extraArgs tracePath concurrentSessions totalSessions; do
  if grep -q "params\.${P})" "${TASK}"; then
    fail "task still references \$(params.${P}) — should be inside observeArgs"
  else
    pass "\$(params.${P}) is gone"
  fi
done

# The branch itself. Corpus/spec selection is the renderer's job now.
echo "${RUN}" | grep -q 'if ' \
  && fail "run-observe still has a conditional — the kind branch should be gone" \
  || pass "run-observe has no conditional"

for FLAG in --corpus-header --corpus-data --concurrent-sessions --workload-spec \
            --post-hoc-detector --max-concurrency; do
  if echo "${RUN}" | grep -q -- "${FLAG}"; then
    fail "run-observe still builds ${FLAG} — should come in via observeArgs"
  else
    pass "run-observe does not build ${FLAG}"
  fi
done

# --server-url stays the task's job: endpoint is a runtime task result.
echo "${RUN}" | grep -q -- '--server-url' \
  && pass "run-observe still appends --server-url" \
  || fail "run-observe lost --server-url"

# Unquoted expansion is load-bearing: quoting it would pass the whole argv as a
# single argument and blis would reject it.
echo "${RUN}" | grep -q '\${OBSERVE_ARGS}' \
  && pass "run-observe expands OBSERVE_ARGS" \
  || fail "run-observe never expands OBSERVE_ARGS"
echo "${RUN}" | grep -q '"\${OBSERVE_ARGS}"' \
  && fail "OBSERVE_ARGS is quoted — argv would be passed as one argument" \
  || pass "OBSERVE_ARGS is unquoted (word-splitting preserved)"

# An unquoted expansion globs as well as splits. `set -f` must come BEFORE the
# invocation, or a rendered * ? or [ matches files in the step's cwd instead of
# reaching blis. Anchored on the command at line start so a mention of "set -f"
# in a comment cannot satisfy it, and compared by position so moving the
# invocation above it fails.
RUN_SETF_LINE="$(printf '%s\n' "${RUN}" | grep -n '^[[:space:]]*set -f[[:space:]]*$' | head -1 | cut -d: -f1)"
RUN_INVOKE_LINE="$(printf '%s\n' "${RUN}" | grep -n '^[[:space:]]*"\${BLIS}" observe' | head -1 | cut -d: -f1)"
if [ -n "${RUN_SETF_LINE}" ] && [ -n "${RUN_INVOKE_LINE}" ] \
   && [ "${RUN_SETF_LINE}" -lt "${RUN_INVOKE_LINE}" ]
then pass "run-observe sets -f before expanding OBSERVE_ARGS (no globbing)"
else fail "run-observe does not disable globbing before the invocation (set -f=${RUN_SETF_LINE:-none} invoke=${RUN_INVOKE_LINE:-none})"
fi

# ────────────────────────────────────────────────────────────
# Part 2 — behavioral: the argv the step actually builds
# ────────────────────────────────────────────────────────────
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
RESULTS="run/treatment/wl-a/i1"
mkdir -p "${TMP}/workspace/data/${RESULTS}"
mkdir -p "${TMP}/source/blis"

# One argument per line: this is what makes word boundaries observable.
cat > "${TMP}/source/blis/blis" <<'SH'
#!/bin/sh
printf '%s\n' "$@" > "${ARGV_LOG}"
exit 0
SH
chmod +x "${TMP}/source/blis/blis"

# Render the step: Tekton substitutions -> ${P_*} env refs, /workspace -> sandbox.
# For run-observe the env-ref form is faithful: Tekton's textual substitution
# lands inside OBSERVE_ARGS="..." and a quoted env expansion splits identically.
render() {
  printf '%s\n' "$1" \
    | sed -e "s#\$(params\.\([A-Za-z]*\))#\${P_\1}#g" \
          -e "s#\$(workspaces\.source\.path)#${TMP}/source#g" \
          -e "s#/workspace#${TMP}/workspace#g"
}
render "${RUN}" > "${TMP}/run.sh"

# run_observe <observeArgs> -> argv, one per line, in ${TMP}/argv.log
run_observe() {
  : > "${TMP}/argv.log"
  ARGV_LOG="${TMP}/argv.log" \
  P_observeArgs="$1" P_endpoint="http://gw.ns.svc:8000/v1" P_resultsDir="${RESULTS}" \
    sh "${TMP}/run.sh" > "${TMP}/run.out" 2>&1
}

# assert_argv <label> <expected-file>
assert_argv() {
  if diff -u "$2" "${TMP}/argv.log" > "${TMP}/argv.diff" 2>&1; then
    pass "$1"
  else
    fail "$1"
    cat "${TMP}/argv.diff"
  fi
}

# --- 2a: a corpus-mode (trace) cell ---
# NOTE ON --detectors: the run-observe step this diff replaces hardcoded
# `--post-hoc-detector composite`, and the structural check above asserts that
# spelling is gone. These fixtures deliberately say `--detectors` instead,
# because --post-hoc-detector DOES NOT EXIST at the blis revision sim2real pins.
# inference-sim renamed it in 18c2c926 (#1516) -- the flag is registered as
# "detectors" (cmd/saturation.go) and put on observe by registerDetectorFlags
# (cmd/observe_cmd.go); "composite" is still a valid detector name. sim2real's
# pin bump (#904) moved past that rename, so the deleted line would have made
# `blis observe` exit on `unknown flag`. The renderer (sim2real#900) emits
# --detectors. Do not "restore" --post-hoc-detector here to match the old task.
TRACE_ARGS="--model Qwen/Qwen2.5-7B-Instruct --max-concurrency 10000 --timeout 1800 --prewarm-duration 60s --warmup-requests 50 --detectors composite --corpus-header /workspace/data/traces/abc123.yaml --corpus-data /workspace/data/traces/abc123.csv --concurrent-sessions 8 --total-sessions 64 --trace-header /workspace/data/${RESULTS}/trace_header.yaml --trace-data /workspace/data/${RESULTS}/trace_data.csv --saturation-report /workspace/data/${RESULTS}/saturation.json"

cat > "${TMP}/want_trace.txt" <<EOF
observe
--model
Qwen/Qwen2.5-7B-Instruct
--max-concurrency
10000
--timeout
1800
--prewarm-duration
60s
--warmup-requests
50
--detectors
composite
--corpus-header
/workspace/data/traces/abc123.yaml
--corpus-data
/workspace/data/traces/abc123.csv
--concurrent-sessions
8
--total-sessions
64
--trace-header
/workspace/data/${RESULTS}/trace_header.yaml
--trace-data
/workspace/data/${RESULTS}/trace_data.csv
--saturation-report
/workspace/data/${RESULTS}/saturation.json
--server-url
http://gw.ns.svc:8000/v1
EOF

if run_observe "${TRACE_ARGS}"; then
  assert_argv "corpus-mode: argv is observeArgs word-split, then --server-url" \
              "${TMP}/want_trace.txt"
else
  fail "corpus-mode run-observe exited non-zero"; cat "${TMP}/run.out"
fi

# Non-vacuousness: if the fake were never reached every diff above would be
# comparing against an empty log and would "pass" only by accident.
[ -s "${TMP}/argv.log" ] \
  && pass "fake blis was actually invoked (assertions are not vacuous)" \
  || fail "fake blis never ran — check the harness"

# --- 2b: a generative (synthetic) cell, SAME code path ---
SYNTH_ARGS="--model Qwen/Qwen2.5-7B-Instruct --max-concurrency 10000 --timeout 1800 --prewarm-duration 60s --warmup-requests 50 --detectors composite --workload-spec /workspace/workload.yaml --trace-header /workspace/data/${RESULTS}/trace_header.yaml --trace-data /workspace/data/${RESULTS}/trace_data.csv --saturation-report /workspace/data/${RESULTS}/saturation.json"

cat > "${TMP}/want_synth.txt" <<EOF
observe
--model
Qwen/Qwen2.5-7B-Instruct
--max-concurrency
10000
--timeout
1800
--prewarm-duration
60s
--warmup-requests
50
--detectors
composite
--workload-spec
/workspace/workload.yaml
--trace-header
/workspace/data/${RESULTS}/trace_header.yaml
--trace-data
/workspace/data/${RESULTS}/trace_data.csv
--saturation-report
/workspace/data/${RESULTS}/saturation.json
--server-url
http://gw.ns.svc:8000/v1
EOF

if run_observe "${SYNTH_ARGS}"; then
  assert_argv "spec-mode: argv is observeArgs word-split, then --server-url" \
              "${TMP}/want_synth.txt"
else
  fail "spec-mode run-observe exited non-zero"; cat "${TMP}/run.out"
fi

# --- 2c: --server-url is appended, and appended LAST ---
# The renderer must not emit it (sim2real#900 AC-4); the task must always add it.
TAIL="$(tail -2 "${TMP}/argv.log" | tr '\n' ' ')"
case "${TAIL}" in
  "--server-url http://gw.ns.svc:8000/v1 ")
    pass "--server-url <endpoint> is the final argument pair" ;;
  *) fail "argv does not end with --server-url <endpoint>: ${TAIL}" ;;
esac
SU_COUNT="$(grep -c -- '--server-url' "${TMP}/argv.log")"
[ "${SU_COUNT}" -eq 1 ] \
  && pass "--server-url appears exactly once" \
  || fail "--server-url appears ${SU_COUNT} times, expected 1"

# --- 2d: a multi-word extraArgs tail survives as separate words ---
# extraArgs is rendered last inside observeArgs and is intentionally many words;
# it must not arrive as one argument.
if run_observe "--model m --workload-spec /workspace/workload.yaml --api-format completions --no-streaming"; then
  cat > "${TMP}/want_extra.txt" <<EOF
observe
--model
m
--workload-spec
/workspace/workload.yaml
--api-format
completions
--no-streaming
--server-url
http://gw.ns.svc:8000/v1
EOF
  assert_argv "multi-word tail splits into separate arguments" \
              "${TMP}/want_extra.txt"
else
  fail "run-observe with a multi-word tail exited non-zero"; cat "${TMP}/run.out"
fi

# --- 2e: a glob character reaches blis verbatim, not expanded ---
# The structural check above pins `set -f`'s position; this proves it works.
# Run with a cwd that CONTAINS matches, because a no-match glob passes through
# unchanged even without set -f — testing in an empty directory would pass
# either way and prove nothing.
GLOBDIR="${TMP}/globcwd"
mkdir -p "${GLOBDIR}"
: > "${GLOBDIR}/spec_a.yaml"
: > "${GLOBDIR}/spec_b.yaml"

: > "${TMP}/argv.log"
( cd "${GLOBDIR}" \
  && ARGV_LOG="${TMP}/argv.log" \
     P_observeArgs="--model m --workload-spec spec_*.yaml" \
     P_endpoint="http://gw.ns.svc:8000/v1" P_resultsDir="${RESULTS}" \
     sh "${TMP}/run.sh" > "${TMP}/glob.out" 2>&1 )
GLOB_RC=$?

if [ "${GLOB_RC}" -eq 0 ]; then
  cat > "${TMP}/want_glob.txt" <<EOF
observe
--model
m
--workload-spec
spec_*.yaml
--server-url
http://gw.ns.svc:8000/v1
EOF
  assert_argv "glob char reaches blis verbatim (set -f suppresses expansion)" \
              "${TMP}/want_glob.txt"
  # Name the actual failure mode if it ever regresses.
  if grep -qx 'spec_a.yaml' "${TMP}/argv.log"; then
    fail "the glob expanded against the step's cwd — set -f is not in effect"
  fi
else
  fail "run-observe with a glob in observeArgs exited non-zero"
  cat "${TMP}/glob.out"
fi

# Counter-check that the fixture is capable of expanding, so the assertion above
# is testing set -f rather than a directory that simply had no matches.
EXPANDED="$(cd "${GLOBDIR}" && sh -c 'A="--workload-spec spec_*.yaml"; set -- ${A}; echo $#')"
[ "${EXPANDED}" -eq 3 ] \
  && pass "harness confirms the glob WOULD expand without set -f (3 words)" \
  || fail "harness glob fixture does not expand (${EXPANDED} words) — test is vacuous"

# ────────────────────────────────────────────────────────────
# Part 3 — AC-4: empty workloadSpec still writes an (empty) workload.yaml
# ────────────────────────────────────────────────────────────
# write-workload-spec's heredoc delimiter is quoted, so an ${P_*} env ref would
# NOT expand inside it. Substitute textually instead — which is exactly what
# Tekton does to $(params.X) before the shell ever sees the script.
render_wws() {  # $1 = script, $2 = resultsDir, $3 = workloadSpec literal
  printf '%s\n' "$1" \
    | sed -e "s#\$(params\.workloadSpec)#$3#g" \
          -e "s#\$(params\.resultsDir)#$2#g" \
          -e "s#/workspace#${TMP}/workspace#g"
}

rm -f "${TMP}/workspace/workload.yaml"
render_wws "${WWS}" "${RESULTS}" "" > "${TMP}/wws.sh"
if sh "${TMP}/wws.sh" > "${TMP}/wws.out" 2>&1; then
  if [ -f "${TMP}/workspace/workload.yaml" ]; then
    pass "empty workloadSpec still writes /workspace/workload.yaml"
    # The heredoc emits the substituted (empty) line, so the file is blank
    # rather than absent — the pre-existing behavior corpus cells rely on.
    if [ -s "${TMP}/workspace/workload.yaml" ] \
       && [ -n "$(tr -d ' \n' < "${TMP}/workspace/workload.yaml")" ]; then
      fail "workload.yaml has content for an empty workloadSpec: $(cat "${TMP}/workspace/workload.yaml")"
    else
      pass "workload.yaml is blank for an empty workloadSpec"
    fi
  else
    fail "workload.yaml not written for an empty workloadSpec"
  fi
  [ -f "${TMP}/workspace/data/${RESULTS}/epp_log_since" ] \
    && pass "write-workload-spec still writes the epp_log_since marker" \
    || fail "epp_log_since marker missing"
else
  fail "write-workload-spec exited non-zero on an empty spec"; cat "${TMP}/wws.out"
fi

# ...and run-observe never reads it, so a blank file is harmless. The path may
# only appear via observeArgs (renderer's choice), never hardcoded in the step.
echo "${RUN}" | grep -q 'workload\.yaml' \
  && fail "run-observe hardcodes workload.yaml — spec-mode must come via observeArgs" \
  || pass "run-observe does not reference workload.yaml"

echo
if ${PASS}; then echo "ALL PASS"; else echo "FAILURES"; exit 1; fi
