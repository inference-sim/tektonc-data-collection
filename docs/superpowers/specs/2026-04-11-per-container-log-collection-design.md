# Per-Container Log Collection in collect-results

**Date:** 2026-04-11  
**Status:** Approved  
**Scope:** `tekton/tasks/collect-results.yaml`

## Problem

The `collect-logs` step in `collect-results.yaml` collects pod logs using `kubectl logs` without targeting specific containers:

- EPP pods: `kubectl logs "${pod}" ... --all-containers=false` — explicitly collects only the first/default container.
- vLLM decode pods: `kubectl logs "${pod}" ...` — no `--all-containers` flag, also defaults to the first container.

Init containers are never collected. Multi-container pods produce an incomplete log file.

## Goal

For every pod (EPP and vLLM decode), collect one log file per container — including init containers — named `${pod}_${ctr}.log`.

## Design

### Approach

Two-query loop (Approach A): for each pod, issue two `kubectl get pod -o jsonpath` calls to enumerate init containers and regular containers separately, then loop over all of them collecting one log file per container.

```sh
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
```

This pattern replaces the existing single `kubectl logs` call in both the EPP and vLLM sections of the step.

### Output file naming

| Container type | Filename |
|----------------|----------|
| Regular container | `${pod}_${ctr}.log` |
| Init container | `${pod}_init-${ctr}.log` |

The `init-` prefix on init container files prevents silent overwrites if an init container and a regular container share the same name (valid in Kubernetes). The old single-file-per-pod output (`${pod}.log`) is removed — it contained incomplete data so no backward-compatible alias is warranted.

### Edge cases

- **No init containers:** `.spec.initContainers[*].name` returns empty string; `for ctr in ${INIT_CTRS}` with an empty variable is a no-op in POSIX sh — no special-casing needed.
- **Container with no logs / crashed container:** `kubectl logs` writes its error message into the `.log` file (stderr redirected via `2>&1`). The loop continues; the step is already under `set +e`.
- **Pod not found during enumeration:** `2>/dev/null || true` suppresses errors; CTRS/INIT_CTRS will be empty and the inner loop is a no-op.

### Error handling

No change to error handling strategy. The step runs under `set +e` (failures are non-fatal to the pipeline). Per-container failures produce an empty or error-message log file rather than aborting collection for the remaining containers.

## Scope

**In scope:**
- `collect-results.yaml` — `collect-logs` step, both EPP and vLLM decode sections.

**Out of scope:**
- `collect-kv-events.yaml` — already targets a specific container by name; its "first pod only" issue is a separate concern.
- Any downstream analysis tooling that reads log files — consumers must be updated separately if they depend on the old `${pod}.log` filename.

## Files Changed

| File | Change |
|------|--------|
| `tekton/tasks/collect-results.yaml` | Replace single `kubectl logs` with two-query per-container loop in both EPP and vLLM sections |
