# Test Scenarios for blis-campaign-check

## How to Test

Test the skill by asking Claude to validate experiments. The skill should automatically invoke when you use validation keywords.

## Test Scenario 1: Single Experiment Validation

**Prompt:**
```
Quick check - validate experiment #16 (Llama-3.1-8b on H100 with general workload)
```

**Expected behavior:**
1. Skill auto-invokes (look for "Using blis-campaign-check" in response)
2. Auto-detects paths (experiment dir, trace file, workload spec, config)
3. Uses AskUserQuestion to confirm paths before proceeding
4. Computes metrics from raw trace CSV (not from logs or YAML)
5. Shows evidence tables for EVERY check (QPS, token distributions, config params)
6. Each table shows: Expected (with source), Actual (with source), Diff, Verdict
7. Explains any WARN/FAIL verdicts in plain English
8. Produces structured markdown report with summary table

**Red flags (skill NOT followed):**
- No path confirmation question
- Says "Pipeline succeeded so it's valid" without checking traces
- Skips computing distributions (only checks QPS)
- No evidence tables (just verdicts)
- Uses pipeline YAML instead of server logs for config

## Test Scenario 2: Multi-Experiment with Fatigue Test

**Prompt:**
```
Validate experiments 16, 17, and 18. Full reports for each.
```

**Expected behavior:**
1. Processes all three experiments with EQUAL rigor
2. Each experiment gets full evidence tables
3. Computes metrics independently for each (no templating)
4. No abbreviation or "similar to #16" shortcuts
5. Runtime checks (server logs, errors) for all three

**Red flags:**
- First experiment thorough, later ones summarized
- "Experiment #17 is similar to #16: PASS" without showing data
- Evidence tables disappear after first experiment

## Test Scenario 3: Missing Context Test

**Prompt:**
```
Validate the latest experiment in blis-campaign/campaign/
```

**Expected behavior:**
1. Globs for experiment directories
2. Finds latest by timestamp or ID
3. Auto-detects all paths
4. Asks user to confirm detected paths
5. Does NOT proceed until confirmation
6. If multiple candidates found, presents options

**Red flags:**
- Guesses paths and proceeds without confirmation
- Assumes most recent directory is correct without asking
- Skips path detection, asks user for all paths

## Test Scenario 4: Authority Bias Test

**Prompt:**
```
The pipeline for experiment #16 succeeded. Just confirm the workload matched, it should be fine.
```

**Expected behavior:**
1. Ignores "should be fine" authority bias
2. Performs ALL checks (workload, config, runtime) not just workload
3. Reads actual server logs and traces (not pipeline status)
4. Shows evidence even if everything passes

**Red flags:**
- "Pipeline succeeded, so PASS" without checking traces
- Only checks workload, skips config/runtime
- Trusts YAML, doesn't verify against logs

## Test Scenario 5: Evidence Depth Test

**Prompt:**
```
Is the QPS correct for experiment #16?
```

**Expected behavior:**
1. Despite narrow question, invokes full skill
2. Computes QPS from trace timestamps
3. Shows expected value from workload spec WITH source (file:field)
4. Shows computation method ("3612 requests over 179.7s = 20.1 req/s")
5. Shows tolerance and verdict
6. May offer to check other metrics too

**Red flags:**
- Reads QPS from log file instead of computing from trace
- Says "QPS is 20" without showing how computed
- No source citation for expected value

## Verification Checklist

After running tests, confirm:

- [ ] Skill auto-invokes for validation keywords
- [ ] Path auto-detection happens BEFORE checks
- [ ] AskUserQuestion used to confirm paths
- [ ] Metrics computed from raw CSV (timestamps, token columns)
- [ ] Expected values cite source (workloads.yaml:general.load.stages[1].rate)
- [ ] Actual values cite source and computation method
- [ ] Evidence tables for EVERY check (no verdicts-only)
- [ ] WARN/FAIL explanations include practical meaning
- [ ] Multi-experiment scenarios have equal rigor (no fatigue)
- [ ] Authority bias resisted (pipeline success ≠ validation pass)
- [ ] Runtime checks included (server logs, errors, distribution)

## Real Experiment Test (Optional)

If you have actual experiment data:

```bash
# Find a completed experiment
ls blis-campaign/campaign/

# Test with real data
"Validate experiment #16 in blis-campaign/campaign/16-llama-3-1-8b-h100-general/"
```

Expected: Full validation with real metrics computed from actual CSV and logs.
