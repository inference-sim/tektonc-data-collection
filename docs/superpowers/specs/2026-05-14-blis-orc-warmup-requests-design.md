# Design: Add Warmup Requests to BLIS ORC Observe Phase

**Date:** 2026-05-14
**Status:** Approved
**Author:** Claude Code

## Problem Statement

The BLIS ORC (Observe-Replay-Calibrate) harness needs to exclude initial warmup requests from trace collection. Cold starts, cache warming, and model initialization can cause atypical latencies in the first few requests, which would skew the observational data used for simulation calibration.

## Solution

Add a fixed `--warmup-requests 50` parameter to the blis observe command in the ORC harness. The blis observe command already supports this flag (implemented in `inference-sim/cmd/observe_cmd.go:128`), which excludes the first N requests from being recorded in the trace output.

## Design

### Implementation Details

**File Modified:** `tekton/tasks/orc-observe.yaml`

**Changes to `run-observe` step (lines 144-151):**

1. Add logging to show warmup configuration:
   ```bash
   echo "🔥 Using warmup-requests=50 (first 50 requests excluded from trace)"
   ```

2. Add `--warmup-requests 50` flag to the blis observe command:
   ```bash
   ${BLIS_BINARY} observe \
     --server-url "${ENDPOINT}" \
     --model "${MODEL}" \
     --workload-spec "${WORKLOAD_SPEC}" \
     --trace-header "header.yaml" \
     --trace-data "data.csv" \
     --warmup-requests 50 \
     --record-itl \
     --itl-output "itl.csv"
   ```

### Behavior

- **Warmup Phase:** The first 50 requests are dispatched to the server at their scheduled arrival times
- **Warmup Effect:** These requests warm up caches, initialize models, and stabilize the system
- **Trace Recording:** Recording begins from request #51 onwards (warmup requests excluded from header.yaml and data.csv)
- **Request Indexing:** The observe command tracks request indices internally and skips recording for `idx < warmupCount` (see `observe_cmd.go:690`)

### Configuration Approach

- **Fixed Default:** 50 warmup requests for all ORC experiments
- **Rationale:** Keeps configuration simple, provides consistent warmup across experiments
- **Future Extension:** If per-experiment customization is needed, this can be made configurable through the pipeline params in a future change

### Files NOT Modified

- `tektoncsample/blis-orc/data_pipeline.yaml.j2` - no changes needed (warmup is internal to task)
- `tektoncsample/blis-orc/values.yaml` - no new parameters required
- `blis-campaign/generate.py` - no workload processing changes needed

## Testing Strategy

1. **Verification Method:**
   - Check task logs for warmup message: `"🔥 Using warmup-requests=50"`
   - Compare total requests sent vs. requests in `data.csv` (should differ by 50)

2. **Backward Compatibility:**
   - Existing ORC experiments continue to work
   - Warmup is additive (doesn't break existing functionality)

3. **Edge Cases:**
   - Workloads with fewer than 50 requests: blis observe handles gracefully (traces will be empty but won't error)
   - Multi-stage workloads: warmup applies to the beginning of the entire observation, not per-stage

## Rationale

### Why 50 Requests?

- Balances sufficient warm-up time with not wasting observation capacity
- Typical cache and initialization effects stabilize within 20-50 requests
- Aligns with common benchmarking practices

### Why Fixed Default?

- Simplicity: no new configuration parameters to maintain
- Consistency: all experiments use the same warmup approach
- Easy to extend: can add configurability later if needed

### Why Log the Value?

- Visibility: operators can verify warmup is being used
- Debugging: helps explain discrepancies between workload spec and trace counts
- Documentation: logs serve as audit trail for experiment configuration

## Implementation Plan

1. Modify `tekton/tasks/orc-observe.yaml`:
   - Add logging line before blis observe command
   - Add `--warmup-requests 50` to command arguments
2. Test on a single ORC experiment
3. Verify logs and trace output
4. Document in commit message

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Workloads with <50 requests produce empty traces | Document behavior; consider adding validation in future |
| Users unaware warmup is happening | Log message makes it explicit |
| Different experiments need different warmup | Make configurable in future if needed |

## References

- **BLIS observe implementation:** `inference-sim/cmd/observe_cmd.go:128` (warmup flag definition)
- **Warmup exclusion logic:** `inference-sim/cmd/observe_cmd.go:690` (skip recording for warmup requests)
- **ORC task definition:** `tekton/tasks/orc-observe.yaml` (where change will be made)
