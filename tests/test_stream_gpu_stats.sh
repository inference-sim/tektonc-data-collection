#!/bin/sh
# Tests the cluster-free parsing logic in the stream-gpu-stats task script:
#   - Phase 0 intervalSeconds validation (regex + bounds)
#   - build_node_dcgm pipe-tuple construction + DCGM_NS caching side effect
#   - cut-d'|' split round-trip used by the sampling loop
#   - sorted node-set equality used by re-resolve change detection
#
# Run with: sh tektonc-data-collection/tests/test_stream_gpu_stats.sh

PASS=true

pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; PASS=false; }

# ────────────────────────────────────────────────────────────
# Phase 0 — intervalSeconds validation
# ────────────────────────────────────────────────────────────
# Mirrors the in-script check:
#   if ! echo "${INTERVAL}" | grep -qE '^[1-9][0-9]{0,2}$' \
#        || [ "${INTERVAL}" -lt 1 ] || [ "${INTERVAL}" -gt 300 ]; then ERROR
validate_interval() {
  v="$1"
  if echo "${v}" | grep -qE '^[1-9][0-9]{0,2}$' \
       && [ "${v}" -ge 1 ] 2>/dev/null && [ "${v}" -le 300 ] 2>/dev/null; then
    echo "ok"
  else
    echo "bad"
  fi
}

[ "$(validate_interval 10)"   = "ok"  ] && pass "interval=10 accepted"     || fail "interval=10 rejected"
[ "$(validate_interval 1)"    = "ok"  ] && pass "interval=1 accepted"      || fail "interval=1 rejected"
[ "$(validate_interval 300)"  = "ok"  ] && pass "interval=300 accepted"    || fail "interval=300 rejected"
[ "$(validate_interval 0)"    = "bad" ] && pass "interval=0 rejected"      || fail "interval=0 accepted"
[ "$(validate_interval 301)"  = "bad" ] && pass "interval=301 rejected"    || fail "interval=301 accepted"
[ "$(validate_interval 1000)" = "bad" ] && pass "interval=1000 rejected"   || fail "interval=1000 accepted"
[ "$(validate_interval abc)"  = "bad" ] && pass "interval=abc rejected"    || fail "interval=abc accepted"
[ "$(validate_interval "")"   = "bad" ] && pass "interval='' rejected"     || fail "interval='' accepted"
[ "$(validate_interval -5)"   = "bad" ] && pass "interval=-5 rejected"     || fail "interval=-5 accepted"

# ────────────────────────────────────────────────────────────
# build_node_dcgm — pipe-tuple construction + DCGM_NS caching
# ────────────────────────────────────────────────────────────
# Stub resolve_dcgm_for_node: maps node -> "ns name" output (mirrors what
# the kubectl get -o custom-columns invocation returns: two whitespace-sep
# columns, NS NAME).
resolve_dcgm_for_node() {
  case "$1" in
    node-a) echo "nvidia-gpu-operator dcgm-exporter-aaaa" ;;
    node-b) echo "nvidia-gpu-operator dcgm-exporter-bbbb" ;;
    node-c) echo "" ;;  # node with no DCGM pod (CPU-only or DCGM crashed)
    *)      echo "" ;;
  esac
}

# Drop-in copy of build_node_dcgm from the task script.
build_node_dcgm() {
  BND_OUT=""
  for bnd_node in $1; do
    bnd_r=$(resolve_dcgm_for_node "${bnd_node}")
    if [ -z "${bnd_r}" ]; then
      continue
    fi
    bnd_ns=$(echo "${bnd_r}" | awk '{print $1}')
    bnd_pod=$(echo "${bnd_r}" | awk '{print $2}')
    BND_OUT="${BND_OUT} ${bnd_node}|${bnd_ns}|${bnd_pod}"
    if [ -z "${DCGM_NS}" ]; then
      DCGM_NS="${bnd_ns}"
    fi
  done
  NODE_DCGM="${BND_OUT}"
}

# Case 1: empty cache, two resolvable nodes — DCGM_NS gets cached, NODE_DCGM
# has both tuples, missing-DCGM nodes are skipped.
DCGM_NS=""
NODE_DCGM=""
build_node_dcgm "node-a node-b node-c"
expected=" node-a|nvidia-gpu-operator|dcgm-exporter-aaaa node-b|nvidia-gpu-operator|dcgm-exporter-bbbb"
[ "${NODE_DCGM}" = "${expected}" ] \
  && pass "build_node_dcgm tuples match for node-a + node-b" \
  || fail "build_node_dcgm tuples mismatch: '${NODE_DCGM}'"
[ "${DCGM_NS}" = "nvidia-gpu-operator" ] \
  && pass "DCGM_NS cached from first successful resolve" \
  || fail "DCGM_NS not cached, got: '${DCGM_NS}'"

# Case 2: pre-set DCGM_NS is preserved (caller-supplied namespace wins).
DCGM_NS="my-explicit-ns"
NODE_DCGM=""
build_node_dcgm "node-a"
[ "${DCGM_NS}" = "my-explicit-ns" ] \
  && pass "DCGM_NS preserved when caller pre-set it" \
  || fail "DCGM_NS overwritten despite pre-set, got: '${DCGM_NS}'"

# Case 3: zero resolvable nodes — NODE_DCGM is empty, signalling the caller
# to break the sampling loop (the in-task exit-when-empty guard).
DCGM_NS=""
NODE_DCGM=""
build_node_dcgm "node-c node-z"
[ -z "${NODE_DCGM}" ] \
  && pass "NODE_DCGM empty when no nodes resolve" \
  || fail "NODE_DCGM should be empty, got: '${NODE_DCGM}'"

# ────────────────────────────────────────────────────────────
# Sampling-loop split: cut -d'|' -f{1,2,3}
# ────────────────────────────────────────────────────────────
entry="node-a|nvidia-gpu-operator|dcgm-exporter-aaaa"
[ "$(echo "${entry}" | cut -d'|' -f1)" = "node-a" ]              && pass "cut -f1 == node"  || fail "cut -f1 wrong"
[ "$(echo "${entry}" | cut -d'|' -f2)" = "nvidia-gpu-operator" ] && pass "cut -f2 == ns"    || fail "cut -f2 wrong"
[ "$(echo "${entry}" | cut -d'|' -f3)" = "dcgm-exporter-aaaa" ]  && pass "cut -f3 == pod"   || fail "cut -f3 wrong"

# Iterating space-separated tuples must yield each tuple as a single token.
DCGM_NS=""
NODE_DCGM=""
build_node_dcgm "node-a node-b"
count=0
for entry in ${NODE_DCGM}; do
  count=$((count + 1))
done
[ "${count}" -eq 2 ] \
  && pass "for-loop over NODE_DCGM yields one token per tuple" \
  || fail "expected 2 tuples in iteration, got: ${count}"

# ────────────────────────────────────────────────────────────
# Re-resolve change detection: sorted node-set equality
# ────────────────────────────────────────────────────────────
# Mirrors the in-script comparison:
#   NODES=$(echo "${PODS_NODES}" | awk '{print $2}' | sort -u | grep -v '^$')
extract_nodes() { echo "$1" | awk '{print $2}' | sort -u | grep -v '^$'; }

OLD_TABLE="vllm-decode-1 node-a
vllm-decode-2 node-b
vllm-prefill-1 node-a"
SAME_TABLE="vllm-decode-1 node-a
vllm-prefill-1 node-a
vllm-decode-2 node-b"
DIFFERENT_TABLE="vllm-decode-1 node-a
vllm-decode-2 node-c"

OLD=$(extract_nodes "${OLD_TABLE}")
SAME=$(extract_nodes "${SAME_TABLE}")
DIFF=$(extract_nodes "${DIFFERENT_TABLE}")

[ "${OLD}" = "${SAME}" ] \
  && pass "node-set equal under input reordering (sort -u canonicalizes)" \
  || fail "reordering changed node set: '${OLD}' vs '${SAME}'"

[ "${OLD}" != "${DIFF}" ] \
  && pass "node-set differs when a node moves" \
  || fail "node-set mismatch not detected: '${OLD}' == '${DIFF}'"

# Empty input must produce empty output (the empty-NEW_PN guard relies on
# this to break the loop without re-evaluating NEW_NODES).
[ -z "$(extract_nodes "")" ] \
  && pass "extract_nodes('') is empty" \
  || fail "extract_nodes('') should be empty"

# ────────────────────────────────────────────────────────────
if ${PASS}; then
  echo ""
  echo "All stream-gpu-stats parsing tests passed."
  exit 0
else
  echo ""
  echo "One or more stream-gpu-stats parsing tests FAILED."
  exit 1
fi
