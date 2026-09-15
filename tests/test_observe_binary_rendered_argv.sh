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
#   - Part 3: this issue's AC-4 — workloadSpec empty still writes
#     /workspace/workload.yaml empty, and run-observe never reads it.
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

# Everything below asserts what the step DOES, so strip comment lines first.
# Grepping the raw script would match a flag merely named in prose -- the same
# trap test_prepare_trace_format_dispatch.sh calls out ("anchor on the COMMAND,
# not on prose"), and one this file hit once the guard's comment mentioned blis
# flags by name.
RUN_CODE="$(printf '%s\n' "${RUN}" | grep -v '^[[:space:]]*#')"

# The kind branch itself. Corpus/spec selection is the renderer's job now.
# Asserted as the absence of if/else rather than of the word "if": the guard
# above is a legitimate conditional, so a blanket "no if" check would either
# fail on it or have to be weakened into meaninglessness. What must not come
# back is a two-armed branch selecting a workload source, plus the WL_ARGS
# accumulator it built into.
echo "${RUN_CODE}" | grep -q '^[[:space:]]*else' \
  && fail "run-observe has an else branch — the kind branch should be gone" \
  || pass "run-observe has no else branch"
echo "${RUN_CODE}" | grep -q 'WL_ARGS' \
  && fail "run-observe still builds a WL_ARGS accumulator" \
  || pass "run-observe has no WL_ARGS accumulator"

for FLAG in --corpus-header --corpus-data --concurrent-sessions --workload-spec \
            --post-hoc-detector --max-concurrency; do
  if echo "${RUN_CODE}" | grep -q -- "${FLAG}"; then
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
# single argument and blis would reject it. Scope the quoting check to the
# INVOCATION line -- the emptiness guard quotes OBSERVE_ARGS on purpose (it wants
# one word there), and a whole-script grep cannot tell the two uses apart.
RUN_INVOKE="$(printf '%s\n' "${RUN_CODE}" | grep '^[[:space:]]*"\${BLIS}" observe')"
[ -n "${RUN_INVOKE}" ] \
  && pass "found the blis invocation line" \
  || fail "cannot find the blis invocation line — later checks would be vacuous"
echo "${RUN_INVOKE}" | grep -q '\${OBSERVE_ARGS}' \
  && pass "the invocation expands OBSERVE_ARGS" \
  || fail "the invocation never expands OBSERVE_ARGS"
echo "${RUN_INVOKE}" | grep -q '"\${OBSERVE_ARGS}"' \
  && fail "OBSERVE_ARGS is quoted at the invocation — argv would be one argument" \
  || pass "OBSERVE_ARGS is unquoted at the invocation (word-splitting preserved)"

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
# spelling is gone. These fixtures deliberately say `--detectors` instead. Do
# NOT "restore" --post-hoc-detector here to match the old task.
#
# Why, and how to re-check it -- none of this is verifiable from inside this
# repo, so verify it rather than trusting this comment. In an inference-sim
# checkout at the revision install-blis builds (sim2real passes it as
# blis_git_commit; sim2real's inference-sim submodule pointer is the same SHA):
#
#     git grep -n "post-hoc" -- '*.go'      # no flag definition, only prose
#     git grep -n '"detectors"' -- '*.go'   # cmd/saturation.go registers it
#     git grep -n registerDetectorFlags     # cmd/observe_cmd.go puts it on observe
#
# At the SHA current when this test was written, --post-hoc-detector did not
# exist and Cobra errors on unknown flags, so the deleted line would have made
# `blis observe` exit before doing any work. If a future pin predates that
# rename, the greps above will show it and this fixture is what needs updating.
# The renderer (sim2real#900) is the component that chooses the spelling.
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

# Non-vacuousness. Note the diff above would already catch an unreached fake --
# it compares against a NON-empty expected file, so an empty log fails loudly.
# This guards the narrower case the diff cannot distinguish: a future edit that
# makes the expected file empty too, leaving nothing actually asserted.
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
# The renderer must not emit it (that is sim2real#900's own fourth criterion,
# a different issue in a different repo); the task must always add it.
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

# --- 2d2: an empty observeArgs fails in the task, naming the real cause ---
# Tekton's "required" only means supplied, so "" still reaches the step. blis
# would fatal on its own, but on a blis flag name rather than on the fact that
# the renderer emitted nothing. Assert the task refuses first: non-zero, blis
# never invoked, and the message names observeArgs.
if run_observe ""; then
  fail "empty observeArgs was accepted — the task should refuse before invoking blis"
else
  pass "empty observeArgs exits non-zero"
  [ -s "${TMP}/argv.log" ] \
    && fail "blis was invoked despite an empty observeArgs" \
    || pass "blis was never invoked for an empty observeArgs"
  grep -q 'observeArgs is empty' "${TMP}/run.out" \
    && pass "the failure message names observeArgs as the cause" \
    || { fail "failure message does not name observeArgs"; cat "${TMP}/run.out"; }
fi

# A whitespace-only value is the same defect wearing a disguise: [ -n ] alone
# would accept it, so this pins that the guard is not merely a length check.
if run_observe "   "; then
  fail "whitespace-only observeArgs was accepted"
else
  pass "whitespace-only observeArgs exits non-zero"
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
# Part 3 — this issue's (tektonc#70) AC-4: empty workloadSpec still writes an
# (empty) workload.yaml
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
