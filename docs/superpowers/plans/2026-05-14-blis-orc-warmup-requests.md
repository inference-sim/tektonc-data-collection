# Add Warmup Requests to BLIS ORC Observe Phase - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `--warmup-requests 50` flag to blis observe command in ORC harness to exclude initial warmup requests from trace collection.

**Architecture:** Modify the `run-observe` step in the ORC observe Tekton task to pass the warmup flag to the blis observe command. Add logging to document the warmup configuration.

**Tech Stack:** Tekton Pipeline YAML, Bash scripting, BLIS observe CLI

---

## Scope Check

This is a focused change to a single Tekton task definition. No decomposition needed.

## File Structure

**Modified:**
- `tekton/tasks/orc-observe.yaml` - Add warmup flag to blis observe command (lines 140-151 in run-observe step)

**No files created** - this is a modification-only change.

---

## Task 1: Add Warmup Flag to ORC Observe Task

**Files:**
- Modify: `tekton/tasks/orc-observe.yaml:140-151`

- [ ] **Step 1: Read the current orc-observe task**

Verify the current structure before making changes:

```bash
cat tekton/tasks/orc-observe.yaml
```

Expected: Should see the `run-observe` step starting around line 89, with the blis observe command around lines 144-151.

- [ ] **Step 2: Add warmup logging and flag to the observe command**

Add the log line before the blis observe command and add the `--warmup-requests 50` flag to the command arguments.

Location: In the `run-observe` step script, after line 142 (after the echo of the BLIS observe command), add:

```bash
echo "🔥 Using warmup-requests=50 (first 50 requests excluded from trace)"
```

Then modify the blis observe command (lines 144-151) to include the warmup flag:

```bash
${BLIS_BINARY} observe \
  --server-url "${ENDPOINT}" \
  --model "${MODEL}" \
  --workload-spec "${WORKLOAD_SPEC}" \
  --trace-header "header.yaml" \
  --trace-data "data.csv" \
  --warmup-requests 50 \
  --record-itl \
  --itl-output "itl.csv" \
  > >(tee -a stdout.log) \
  2> >(tee -a stderr.log >&2)
```

The new flag should be inserted between `--trace-data "data.csv"` and `--record-itl` for logical grouping (trace configuration together, then warmup, then ITL options).

- [ ] **Step 3: Verify YAML syntax is valid**

Run a YAML validation check:

```bash
python3 -c "import yaml; yaml.safe_load(open('tekton/tasks/orc-observe.yaml'))"
```

Expected: No output (successful validation), or if kubectl is available:

```bash
kubectl apply --dry-run=client -f tekton/tasks/orc-observe.yaml
```

Expected: `task.tekton.dev/orc-observe configured (dry run)` or similar success message.

- [ ] **Step 4: Verify the change is correct**

Check that both modifications are present:

```bash
grep -A 2 "warmup-requests" tekton/tasks/orc-observe.yaml
```

Expected: Should show the log line and the command flag:
```
        echo "🔥 Using warmup-requests=50 (first 50 requests excluded from trace)"
        ...
          --warmup-requests 50 \
```

- [ ] **Step 5: Commit the change**

```bash
git add tekton/tasks/orc-observe.yaml
git commit -m "feat(orc): add 50-request warmup to blis observe

Add --warmup-requests 50 flag to exclude initial warmup requests from
trace collection. First 50 requests sent to server for cache warming
but not recorded in output traces (header.yaml/data.csv).

Addresses cold start latency skew in observational data.

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

## Task 2: Verification (Optional - For Local Testing)

**Note:** This task is optional and only applicable if you have access to a Tekton cluster for testing.

**Files:**
- Read: `tekton/tasks/orc-observe.yaml` (deployed task)
- Read: Generated pipeline run logs

- [ ] **Step 1: Deploy the updated task**

If testing on a cluster:

```bash
kubectl apply -f tekton/tasks/orc-observe.yaml
```

Expected: `task.tekton.dev/orc-observe configured`

- [ ] **Step 2: Run a test ORC experiment**

Use an existing experiment or create a minimal test:

```bash
# Example using blis-campaign generate
python blis-campaign/generate.py --experiments experiments.json --output test-campaign/
kubectl apply -f test-campaign/<experiment-name>/pipeline.yaml
kubectl apply -f test-campaign/<experiment-name>/pipelinerun.yaml
```

Expected: Pipeline starts successfully.

- [ ] **Step 3: Check task logs for warmup message**

Monitor the orc-observe task logs:

```bash
# Get the pipeline run name
PIPELINERUN=$(kubectl get pr -l tekton.dev/pipeline=<pipeline-name> --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1].metadata.name}')

# Get the orc-observe task run logs
kubectl logs -f $(kubectl get tr -l tekton.dev/pipelineRun=$PIPELINERUN,tekton.dev/pipelineTask=orc-observe-1-1 -o jsonpath='{.items[0].metadata.name}') -c step-run-observe
```

Expected: Should see the log line:
```
🔥 Using warmup-requests=50 (first 50 requests excluded from trace)
```

And the command output should include:
```
blis observe ... --warmup-requests 50 ...
```

- [ ] **Step 4: Verify trace output has correct request count**

After the observe task completes, download the data and verify:

```bash
# Download observe data from the pipeline
# (Exact commands depend on your cluster setup)

# Count requests in trace
wc -l data.csv
```

Expected: If the workload generated N requests, the trace should have (N - 50) requests recorded.

---

## Self-Review Checklist

**Spec Coverage:**
- ✅ Add logging line (Design section, point 1)
- ✅ Add `--warmup-requests 50` flag (Design section, point 2)
- ✅ No changes to pipeline templates, values files, or generate.py (Files NOT Modified section)
- ✅ Testing strategy covered in optional Task 2 (Testing Strategy section)

**Placeholder Scan:**
- ✅ No TBD, TODO, or "implement later"
- ✅ No "add appropriate error handling" without specifics
- ✅ No "write tests" without actual test code
- ✅ All code blocks are complete and specific

**Type Consistency:**
- ✅ Flag name consistent: `--warmup-requests 50` (used consistently in spec and plan)
- ✅ Log message consistent: `🔥 Using warmup-requests=50 (first 50 requests excluded from trace)`
- ✅ File paths consistent: `tekton/tasks/orc-observe.yaml`

**Task Completeness:**
- ✅ Task 1 has exact file locations
- ✅ Task 1 has complete code for modifications
- ✅ Task 1 has verification commands with expected output
- ✅ Task 1 has commit message
- ✅ Task 2 (optional) has verification steps for cluster testing

---

## Notes

- **Backward Compatibility:** This change is additive and doesn't break existing pipelines. All existing ORC experiments will automatically use the 50-request warmup on next run.

- **Edge Cases:** If a workload has fewer than 50 requests, the trace will be empty or have very few requests. The blis observe command handles this gracefully without errors. Document this behavior if it becomes an issue in practice.

- **Future Extension:** If per-experiment warmup customization is needed, add a pipeline parameter and pass it through the template. For now, the fixed default keeps things simple.
