# Per-Container Log Collection Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single-container `kubectl logs` calls in `collect-results.yaml` with per-container loops that collect one log file per container (including init containers).

**Architecture:** The `collect-logs` step in `collect-results.yaml` currently calls `kubectl logs <pod>` once per pod, writing a single `${pod}.log`. The change enumerates init containers and regular containers separately via `kubectl get pod -o jsonpath`, then writes `${pod}_init-${ctr}.log` and `${pod}_${ctr}.log` respectively.

**Tech Stack:** Bash/POSIX sh, kubectl, Tekton Task YAML.

---

## Chunk 1: Edit collect-results.yaml

### Task 1: Replace EPP log collection loop

**Files:**
- Modify: `tekton/tasks/collect-results.yaml` (collect-logs step, EPP section, lines 56–60)

The current EPP section:
```sh
for pod in ${EPP_PODS}; do
  echo "Collecting EPP log: ${pod}"
  kubectl logs "${pod}" -n "${NAMESPACE}" --all-containers=false \
    > "${RESULTS_DIR}/${pod}.log" 2>&1
done
```

Replace with:
```sh
for pod in ${EPP_PODS}; do
  echo "Collecting EPP logs: ${pod}"
  INIT_CTRS=$(kubectl get pod "${pod}" -n "${NAMESPACE}" \
    -o jsonpath='{.spec.initContainers[*].name}' 2>/dev/null || true)
  CTRS=$(kubectl get pod "${pod}" -n "${NAMESPACE}" \
    -o jsonpath='{.spec.containers[*].name}' 2>/dev/null || true)
  for ctr in ${INIT_CTRS}; do
    kubectl logs "${pod}" -n "${NAMESPACE}" -c "${ctr}" \
      > "${RESULTS_DIR}/${pod}_init-${ctr}.log" 2>&1
  done
  for ctr in ${CTRS}; do
    kubectl logs "${pod}" -n "${NAMESPACE}" -c "${ctr}" \
      > "${RESULTS_DIR}/${pod}_${ctr}.log" 2>&1
  done
done
```

- [ ] **Step 1: Open and read the current file**

  Read `tekton/tasks/collect-results.yaml` and locate the EPP section (the `for pod in ${EPP_PODS}` loop inside `collect-logs`).

- [ ] **Step 2: Apply the EPP edit**

  Replace the EPP for-loop body as shown above. The surrounding structure (the `if [ -z "${EPP_PODS}" ]` guard and the outer `for pod in ${EPP_PODS}`) stays unchanged.

- [ ] **Step 3: Verify shell syntax**

  Extract just the new shell block and run it through `bash -n` to check for syntax errors:

  ```bash
  bash -n - <<'EOF'
  #!/bin/sh
  set +e
  NAMESPACE="ns"
  RESULTS_DIR="/tmp/results"
  EPP_PODS="pod-a pod-b"
  for pod in ${EPP_PODS}; do
    echo "Collecting EPP logs: ${pod}"
    INIT_CTRS=$(kubectl get pod "${pod}" -n "${NAMESPACE}" \
      -o jsonpath='{.spec.initContainers[*].name}' 2>/dev/null || true)
    CTRS=$(kubectl get pod "${pod}" -n "${NAMESPACE}" \
      -o jsonpath='{.spec.containers[*].name}' 2>/dev/null || true)
    for ctr in ${INIT_CTRS}; do
      kubectl logs "${pod}" -n "${NAMESPACE}" -c "${ctr}" \
        > "${RESULTS_DIR}/${pod}_init-${ctr}.log" 2>&1
    done
    for ctr in ${CTRS}; do
      kubectl logs "${pod}" -n "${NAMESPACE}" -c "${ctr}" \
        > "${RESULTS_DIR}/${pod}_${ctr}.log" 2>&1
    done
  done
  EOF
  ```

  Expected: no output, exit 0.

---

### Task 2: Replace vLLM decode log collection loop

**Files:**
- Modify: `tekton/tasks/collect-results.yaml` (collect-logs step, vLLM section, lines 71–75)

The current vLLM section:
```sh
for pod in ${DECODE_PODS}; do
  echo "Collecting vLLM log: ${pod}"
  kubectl logs "${pod}" -n "${NAMESPACE}" \
    > "${RESULTS_DIR}/${pod}.log" 2>&1
done
```

Replace with:
```sh
for pod in ${DECODE_PODS}; do
  echo "Collecting vLLM logs: ${pod}"
  INIT_CTRS=$(kubectl get pod "${pod}" -n "${NAMESPACE}" \
    -o jsonpath='{.spec.initContainers[*].name}' 2>/dev/null || true)
  CTRS=$(kubectl get pod "${pod}" -n "${NAMESPACE}" \
    -o jsonpath='{.spec.containers[*].name}' 2>/dev/null || true)
  for ctr in ${INIT_CTRS}; do
    kubectl logs "${pod}" -n "${NAMESPACE}" -c "${ctr}" \
      > "${RESULTS_DIR}/${pod}_init-${ctr}.log" 2>&1
  done
  for ctr in ${CTRS}; do
    kubectl logs "${pod}" -n "${NAMESPACE}" -c "${ctr}" \
      > "${RESULTS_DIR}/${pod}_${ctr}.log" 2>&1
  done
done
```

- [ ] **Step 1: Apply the vLLM edit**

  Replace the vLLM for-loop body as shown above. The surrounding `if [ -z "${DECODE_PODS}" ]` guard and outer `for pod in ${DECODE_PODS}` stay unchanged.

- [ ] **Step 2: Verify shell syntax** (same technique as Task 1, Step 3 — substitute `DECODE_PODS` for `EPP_PODS`)

  Expected: no output, exit 0.

- [ ] **Step 3: Read the final file and do a visual sanity check**

  Confirm:
  - Both sections (EPP and vLLM) have identical structure: two `kubectl get pod -o jsonpath` queries followed by two for-loops.
  - The EPP section writes `${pod}_init-${ctr}.log` / `${pod}_${ctr}.log` (no `--all-containers=false` anywhere).
  - The vLLM section writes `${pod}_init-${ctr}.log` / `${pod}_${ctr}.log`.
  - The echo messages say "logs" (plural) not "log".
  - The file is approximately 18 lines longer than the original 79-line file (each replacement adds ~9 lines).

- [ ] **Step 4: Commit**

  ```bash
  git add tekton/tasks/collect-results.yaml
  git commit -m "feat(collect-results): collect logs for all containers including init containers"
  ```
