#!/bin/sh
# Tests the resource-enumeration label selector in the collect-results task
# (sim2real#893):
#   - Part 1: the selector is present and wired into the enumeration command
#     (this part FAILS if the selector is dropped — the issue's acceptance
#     criterion)
#   - Part 2: the selector's intent, evaluated against the label sets actually
#     observed on a run: Tekton task pods are excluded, the system under test
#     and the harness-support objects are kept
#
# Part 2 reimplements Kubernetes label-selector semantics for the two
# requirements used, so the test pins *which objects survive* rather than only
# the spelling of the string. Notably `key!=value` and `!key` both match an
# object that does not carry the key at all — the property the unlabelled and
# real-workload cases depend on.
#
# Run with: sh tektonc-data-collection/tests/test_collect_resources_selector.sh

PASS=true

pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; PASS=false; }

TASK="$(dirname "$0")/../tekton/tasks/collect-results.yaml"

[ -f "${TASK}" ] || { echo "FAIL: cannot find ${TASK}"; exit 1; }

# ────────────────────────────────────────────────────────────
# Part 1 — the selector is present and actually applied
# ────────────────────────────────────────────────────────────
# The enumeration is the only kubectl call whose output decides which objects
# get collected; the per-object `kubectl get <kind> <name>` that follows is
# addressed by name, so a selector there would be inert. Assert the selector
# lands on the enumeration specifically.
# Anchor structurally on the loop header rather than on the kubectl call, so
# the assertion cannot accidentally match a different step's enumeration (an
# earlier step enumerates decode pods with the same custom-columns flag) and
# so it is not circular with the thing being asserted.
LOOP_LINE=$(grep -n 'for kind in \$(echo "\${KINDS}"' "${TASK}" | head -1 | cut -d: -f1)
[ -n "${LOOP_LINE}" ] \
  && pass "found the resource-kind loop in the task" \
  || fail "could not locate the resource-kind loop"

# The enumeration is the loop's first statement, spanning two physical lines.
ENUM_CMD=$(sed -n "$((LOOP_LINE + 1)),$((LOOP_LINE + 2))p" "${TASK}")

echo "${ENUM_CMD}" | grep -q 'kubectl get "\${kind}"' \
  && pass "loop body begins with the resource enumeration" \
  || fail "expected enumeration as first loop statement, got: ${ENUM_CMD}"

echo "${ENUM_CMD}" | grep -q 'custom-columns=NAME:.metadata.name' \
  && pass "enumeration reads object names" \
  || fail "enumeration does not read object names: ${ENUM_CMD}"

echo "${ENUM_CMD}" | grep -q -- '-l "${SELECTOR}"' \
  && pass "enumeration applies -l \"\${SELECTOR}\"" \
  || fail "enumeration does not apply the selector: ${ENUM_CMD}"

SELECTOR=$(sed -n "s/^ *SELECTOR='\(.*\)' *$/\1/p" "${TASK}")
[ -n "${SELECTOR}" ] \
  && pass "SELECTOR is assigned in the task script" \
  || fail "SELECTOR assignment not found"

# Exact value matters: deploy-gaie.yaml labels its supplemental RBAC
# `app.kubernetes.io/managed-by: tekton` — one word away from this. A selector
# written against the shorter value would exclude nothing here today and could
# exclude real objects later.
echo "${SELECTOR}" | grep -q 'app\.kubernetes\.io/managed-by!=tekton-pipelines' \
  && pass "selector excludes managed-by=tekton-pipelines (exact value)" \
  || fail "selector missing the managed-by requirement: ${SELECTOR}"

echo "${SELECTOR}" | grep -q '!tekton\.dev/taskRun' \
  && pass "selector excludes objects carrying tekton.dev/taskRun" \
  || fail "selector missing the taskRun requirement: ${SELECTOR}"

# The managed-by value must be a not-equal, never an equality — `=` here would
# invert the filter and collect *only* the Tekton pods.
echo "${SELECTOR}" | grep -q 'managed-by=tekton-pipelines' \
  && fail "selector uses = on managed-by; must be != (filter is inverted)" \
  || pass "managed-by requirement is a not-equal, not an equality"

# ────────────────────────────────────────────────────────────
# Part 2 — selector semantics against observed label sets
# ────────────────────────────────────────────────────────────
# Labels are given as space-separated key=value tokens, as `kubectl get
# --show-labels` would render them (comma-separated) after splitting.

# has_label KEY LABELS -> "yes" | "no"
has_label() {
  for hl_tok in $2; do
    case "${hl_tok}" in
      "$1="*) echo "yes"; return ;;
    esac
  done
  echo "no"
}

# label_value KEY LABELS -> value, or empty when the key is absent
label_value() {
  for lv_tok in $2; do
    case "${lv_tok}" in
      "$1="*) echo "${lv_tok#"$1"=}"; return ;;
    esac
  done
  echo ""
}

# matches_selector LABELS -> "collected" | "excluded"
#
# Implements the two requirements in SELECTOR:
#   app.kubernetes.io/managed-by!=tekton-pipelines
#     satisfied when the key is absent, or present with a different value
#   !tekton.dev/taskRun
#     satisfied only when the key is absent
matches_selector() {
  ms_labels="$1"

  if [ "$(has_label 'tekton.dev/taskRun' "${ms_labels}")" = "yes" ]; then
    echo "excluded"; return
  fi

  if [ "$(has_label 'app.kubernetes.io/managed-by' "${ms_labels}")" = "yes" ] \
     && [ "$(label_value 'app.kubernetes.io/managed-by' "${ms_labels}")" \
          = "tekton-pipelines" ]; then
    echo "excluded"; return
  fi

  echo "collected"
}

# --- the measurement apparatus: must be excluded -----------------------------
TEKTON_POD='app.kubernetes.io/managed-by=tekton-pipelines tekton.dev/taskRun=collect-results-abc tekton.dev/pipelineRun=pr-xyz'
[ "$(matches_selector "${TEKTON_POD}")" = "excluded" ] \
  && pass "Tekton task pod excluded" \
  || fail "Tekton task pod was collected"

# A cluster that overrode default-managed-by-label-value: managed-by no longer
# matches, so exclusion rests entirely on the taskRun requirement.
TEKTON_POD_RELABELLED='app.kubernetes.io/managed-by=my-tekton tekton.dev/taskRun=collect-results-abc'
[ "$(matches_selector "${TEKTON_POD_RELABELLED}")" = "excluded" ] \
  && pass "Tekton pod still excluded when managed-by value is overridden" \
  || fail "Tekton pod collected after managed-by override"

# And the converse: managed-by alone excludes even without the taskRun label.
[ "$(matches_selector 'app.kubernetes.io/managed-by=tekton-pipelines')" = "excluded" ] \
  && pass "managed-by=tekton-pipelines alone is enough to exclude" \
  || fail "managed-by-only object was collected"

# --- the system under test: must be kept -------------------------------------
DECODE_POD='app=vllm llm-d.ai/role=decode app.kubernetes.io/managed-by=Helm'
[ "$(matches_selector "${DECODE_POD}")" = "collected" ] \
  && pass "vLLM decode pod collected (managed-by=Helm satisfies !=)" \
  || fail "vLLM decode pod was excluded"

EPP_POD='app=epp inferencepool=default'
[ "$(matches_selector "${EPP_POD}")" = "collected" ] \
  && pass "EPP pod collected" \
  || fail "EPP pod was excluded"

# The near-miss value from deploy-gaie.yaml must NOT be caught.
[ "$(matches_selector 'app.kubernetes.io/managed-by=tekton')" = "collected" ] \
  && pass "managed-by=tekton (not -pipelines) is collected, not caught by mistake" \
  || fail "managed-by=tekton was excluded — selector value is too loose"

# An object with no labels at all: both requirements are satisfied by absence.
[ "$(matches_selector '')" = "collected" ] \
  && pass "unlabelled object collected (absent key satisfies != and !key)" \
  || fail "unlabelled object was excluded"

# --- harness-support objects: kept, by decision (sim2real#893) ---------------
# These carry neither label, so the selector keeps them. Pinned as a decision,
# not an accident: they describe the traffic-generating apparatus, and a
# name-keyed rule to drop them would break on the next object in the namespace.
HARNESS_SVC='app=llm-d-benchmark-harness'
[ "$(matches_selector "${HARNESS_SVC}")" = "collected" ] \
  && pass "harness service kept (documented decision)" \
  || fail "harness service was excluded — decision in #893 was to keep it"

[ "$(matches_selector 'app=harness-data-reader')" = "collected" ] \
  && pass "harness PVC-access pod kept (documented decision)" \
  || fail "harness PVC-access pod was excluded"

# ────────────────────────────────────────────────────────────
if ${PASS}; then
  echo ""
  echo "All collect-results selector tests passed."
  exit 0
else
  echo ""
  echo "One or more collect-results selector tests FAILED."
  exit 1
fi
