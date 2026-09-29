#!/bin/sh
# Asserts that stream-metrics gets kubectl from an image rather than downloading
# it at runtime.
#
# The step runs on python:3.12-slim-bookworm because collect_metrics.sh shells
# out to python3 on every sample, so python and kubectl are both needed in one
# step and cannot be split across images by phase. kubectl therefore arrives from
# a stage-kubectl step on alpine/kubectl:1.34.1 — the image the other three
# kubectl-using tasks run on — over an emptyDir the two steps share, which works
# because Tekton runs a Task's steps as containers in one Pod. Crossing from
# Alpine/musl to Debian/glibc is only safe because kubectl is statically linked.
#
# TASK overrides the file under test, for checking another revision:
#   TASK=/tmp/other.yaml sh tests/test_stream_metrics_kubectl_staging.sh
#
# Run with: sh tektonc-data-collection/tests/test_stream_metrics_kubectl_staging.sh

HERE=$(dirname "$0")
TASK="${TASK:-${HERE}/../tekton/tasks/stream-metrics.yaml}"

PYTHON=python3
for cand in "${HERE}/../../.venv/bin/python" "${HERE}/../.venv/bin/python"; do
  [ -x "${cand}" ] && PYTHON="${cand}" && break
done
"${PYTHON}" -c 'import yaml' 2>/dev/null || {
  echo "SKIP: PyYAML not available for ${PYTHON}"
  exit 0
}

PASS=true
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; PASS=false; }

[ -f "${TASK}" ] || { echo "FAIL: cannot find ${TASK}"; exit 1; }

# ────────────────────────────────────────────────────────────
# No runtime download of kubectl
# ────────────────────────────────────────────────────────────
# The bytes must not cross the network at run time.
#
# Comment lines are stripped before grepping, because prose in the task may name
# the download host legitimately -- the "anchor on the COMMAND, not on prose"
# rule test_prepare_trace_format_dispatch.sh states.
CODE=""
for step in stage-kubectl stream-metrics; do
  CODE="${CODE}
$("${PYTHON}" "${HERE}/lib/extract_step.py" "${TASK}" "${step}" 2>/dev/null \
    | grep -v '^[[:space:]]*#')"
done

if echo "${CODE}" | grep -q 'dl\.k8s\.io'; then
  fail "task still fetches kubectl from dl.k8s.io at runtime"
else
  pass "no runtime kubectl download (no dl.k8s.io in any step's code)"
fi

# And the fetch itself, independent of host: nothing may write a kubectl binary.
if echo "${CODE}" | grep -qE '(curl|wget)[^|]*(-o|-O)[^|]*kubectl'; then
  fail "a step still downloads a kubectl binary"
else
  pass "no step downloads a kubectl binary"
fi

# ────────────────────────────────────────────────────────────
# A staging step supplies it instead
# ────────────────────────────────────────────────────────────
"${PYTHON}" - "${TASK}" <<'PY'
import sys, yaml
task = yaml.safe_load(open(sys.argv[1]))
spec = task["spec"]
steps = {s["name"]: s for s in spec["steps"]}
vols = {v["name"]: v for v in spec.get("volumes", [])}
problems = []

stage = steps.get("stage-kubectl")
main = steps.get("stream-metrics")
if stage is None:
    problems.append("no stage-kubectl step")
if main is None:
    problems.append("no stream-metrics step")

if stage is not None:
    # Must come from an image that ships kubectl, and must be the same pinned
    # version the siblings use -- otherwise this silently changes client skew.
    if "alpine/kubectl:1.34.1" not in stage.get("image", ""):
        problems.append(f"stage-kubectl image is {stage.get('image')!r}, expected alpine/kubectl:1.34.1")
    # Best-effort: a hard failure here would fail the TaskRun from a step that
    # cannot write the bootstrap_fail marker, breaking the non-fatal contract.
    if "set +e" not in stage.get("script", ""):
        problems.append("stage-kubectl does not run with `set +e` (would fail the TaskRun)")

if stage is not None and main is not None:
    sm = {m["name"]: m["mountPath"] for m in stage.get("volumeMounts", [])}
    mm = {m["name"]: m["mountPath"] for m in main.get("volumeMounts", [])}
    shared = set(sm) & set(mm)
    if not shared:
        problems.append("stage-kubectl and stream-metrics share no volume")
    for name in shared:
        if sm[name] != mm[name]:
            problems.append(f"volume {name} mounted at different paths ({sm[name]} vs {mm[name]})")
        v = vols.get(name, {})
        # emptyDir, NOT the data workspace: the results PVC is walked by
        # `deploy.py collect`, so a 60 MB binary there ships to the operator.
        if "emptyDir" not in v:
            problems.append(f"shared volume {name} is not an emptyDir: {sorted(v.keys())}")

if problems:
    print("; ".join(problems), file=sys.stderr)
    sys.exit(1)
sys.exit(0)
PY
if [ $? -eq 0 ]; then
  pass "stage-kubectl (alpine/kubectl:1.34.1, set +e) shares an emptyDir with stream-metrics at the same path"
else
  fail "staging wiring is wrong (see above)"
fi

# ────────────────────────────────────────────────────────────
# The main step uses the staged copy, and refuses to proceed without it
# ────────────────────────────────────────────────────────────
MAIN=$("${PYTHON}" "${HERE}/lib/extract_step.py" "${TASK}" stream-metrics 2>/dev/null)
if [ -z "${MAIN}" ]; then
  fail "could not extract the stream-metrics step"
else
  echo "${MAIN}" | grep -q 'PATH="/tools:' \
    && pass "stream-metrics puts the staged /tools first on PATH" \
    || fail "stream-metrics never puts /tools on PATH — the staged kubectl is unreachable"

  # Staging is best-effort, so the main step must turn a missing binary into the
  # recorded bootstrap_fail marker rather than letting collect_metrics.sh fail
  # per-sample with nothing written to the results tree.
  if echo "${MAIN}" | grep -A2 'command -v kubectl' | grep -q 'bootstrap_fail'; then
    pass "stream-metrics bootstrap_fails when kubectl is absent"
  else
    fail "stream-metrics does not check for kubectl — a failed staging would surface only as per-sample errors"
  fi
fi

${PASS} && { echo "ALL PASS"; exit 0; } || { echo "FAILURES"; exit 1; }
