# Dynamic Workload Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Modify the blis-campaign generator to dynamically generate workloads using combine_workload.py as a module instead of loading from static workloads.yaml.

**Architecture:** Refactor combine_workload.py to make output_file optional and return workload dict. Import it in generate.py and call it for each experiment using the workload and arrival_pattern fields. Replace workloads.yaml validation with arrival-and-workload-patterns.yaml validation.

**Tech Stack:** Python 3, YAML processing, subprocess-free module imports

---

## Scope Check

This is a focused refactoring affecting two files in blis-campaign. No decomposition needed.

## File Structure

**Modified:**
- `blis-campaign/combine_workload.py` - Make output_file optional, return dict
- `blis-campaign/generate.py` - Import module, add dynamic generation, update validation

**No files created** - this is a modification-only refactoring.

---

## Task 1: Refactor combine_workload.py for Module Usage

**Files:**
- Modify: `blis-campaign/combine_workload.py:16-130`

- [ ] **Step 1: Read current combine_workload function**

Verify the current structure:

```bash
grep -A 10 "def combine_workload" blis-campaign/combine_workload.py
```

Expected: Function starts at line 16, currently requires output_file parameter

- [ ] **Step 2: Make output_file parameter optional**

Change the function signature to make output_file optional with default None:

```python
# Old signature (line 16)
def combine_workload(patterns_file, workload_name, arrival_pattern, output_file, seed=42):

# New signature (line 16)
def combine_workload(patterns_file, workload_name, arrival_pattern, output_file=None, seed=42):
```

Update the docstring (lines 17-26) to document the optional parameter:

```python
def combine_workload(patterns_file, workload_name, arrival_pattern, output_file=None, seed=42):
    """
    Combine arrival patterns and workload distributions into a BLIS workload.

    Args:
        patterns_file: Path to arrival-and-workload-patterns.yaml
        workload_name: Name of the workload (e.g., "m-mid")
        arrival_pattern: Name of the arrival pattern (e.g., "morning", "afternoon", "midnight")
        output_file: Path to output BLIS workload file (optional, skip file writing if None)
        seed: Random seed for the workload (default: 42)

    Returns:
        dict: BLIS workload structure with version, seed, cohorts, etc.
    """
```

- [ ] **Step 3: Make file writing conditional**

Modify the file writing section (lines 121-129) to only write when output_file is provided:

```python
# Old code (lines 121-129)
    # Write the output file
    with open(output_file, 'w') as f:
        yaml.dump(blis_workload, f, default_flow_style=False, sort_keys=False)

    print(f"✓ Successfully created BLIS workload file: {output_file}")
    print(f"  Workload: {workload_name}")
    print(f"  Arrival pattern: {arrival_pattern}")
    print(f"  Cohorts: {len(blis_workload['cohorts'])} ({', '.join([c['id'] for c in blis_workload['cohorts']])})\"

# New code (lines 121-129)
    # Write the output file if path provided
    if output_file is not None:
        with open(output_file, 'w') as f:
            yaml.dump(blis_workload, f, default_flow_style=False, sort_keys=False)

        print(f"✓ Successfully created BLIS workload file: {output_file}")
        print(f"  Workload: {workload_name}")
        print(f"  Arrival pattern: {arrival_pattern}")
        print(f"  Cohorts: {len(blis_workload['cohorts'])} ({', '.join([c['id'] for c in blis_workload['cohorts']])})\"
```

- [ ] **Step 4: Return the workload dict**

Ensure the function returns blis_workload at the end (line 130):

```python
    return blis_workload
```

This line already exists, just verify it's present.

- [ ] **Step 5: Verify CLI still works**

Test the standalone CLI functionality:

```bash
python3 blis-campaign/combine_workload.py --list
```

Expected: Lists available workloads and arrival patterns without errors

- [ ] **Step 6: Test module usage**

Test that the function can be called without output_file:

```bash
python3 -c "
import sys
from pathlib import Path
sys.path.insert(0, 'blis-campaign')
from combine_workload import combine_workload

workload = combine_workload(
    patterns_file='blis-campaign/arrival-and-workload-patterns.yaml',
    workload_name='m-mid',
    arrival_pattern='afternoon',
    output_file=None
)
print('Workload version:', workload.get('version'))
print('Cohorts:', len(workload.get('cohorts', [])))
"
```

Expected:
```
Workload version: 2
Cohorts: 3
```
(No file written, function returns dict)

- [ ] **Step 7: Commit the changes**

```bash
git add blis-campaign/combine_workload.py
git commit -m "refactor: make combine_workload output_file optional

Make output_file parameter optional (default None) to enable module
usage. Skip file writing when output_file is None. Function now
returns workload dict for in-memory usage while preserving CLI tool
functionality.

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

## Task 2: Update generate.py for Dynamic Workload Generation

**Files:**
- Modify: `blis-campaign/generate.py:1-520`

- [ ] **Step 1: Add import for combine_workload**

Add import at the top of generate.py (after line 12):

```python
# After existing imports (line 12)
import copy
import json
import re
import subprocess
import sys
import yaml
from pathlib import Path

# Add these lines
sys.path.insert(0, str(Path(__file__).parent))
from combine_workload import combine_workload
```

- [ ] **Step 2: Remove load_workloads function**

Remove the load_workloads function (lines 53-55):

```python
# DELETE THESE LINES (53-55)
def load_workloads(path):
    """Load workloads.yaml config."""
    return load_yaml(path)
```

- [ ] **Step 3: Add generate_workload_for_experiment function**

Add new function after load_clusters (around line 53, where load_workloads was):

```python
def generate_workload_for_experiment(exp, patterns_file):
    """
    Generate workload dynamically for an experiment.

    Args:
        exp: Experiment dict with 'workload' and 'arrival_pattern' fields
        patterns_file: Path to arrival-and-workload-patterns.yaml

    Returns:
        dict: BLIS native workload structure

    Raises:
        ValueError: If workload generation fails
    """
    try:
        workload_data = combine_workload(
            patterns_file=patterns_file,
            workload_name=exp["workload"],
            arrival_pattern=exp["arrival_pattern"],
            output_file=None,  # Don't write to file
            seed=42
        )
        return workload_data
    except ValueError as e:
        raise ValueError(f"Experiment #{exp['id']}: {e}")
    except Exception as e:
        raise RuntimeError(f"Experiment #{exp['id']}: Failed to generate workload: {e}")
```

- [ ] **Step 4: Update validate_all function signature and logic**

Change validate_all function (lines 62-93) to accept patterns_data instead of workloads:

```python
# Old signature (line 62)
def validate_all(experiments, models, clusters, workloads):
    """Validate all experiments. Returns list of error strings (empty = valid)."""

# New signature (line 62)
def validate_all(experiments, models, clusters, patterns_data):
    """
    Validate all experiments. Returns list of error strings (empty = valid).

    Args:
        patterns_data: Dict from arrival-and-workload-patterns.yaml with
                      'arrival_patterns' and 'workloads' keys
    """
```

Update the validation logic inside the function (lines 64-93):

```python
def validate_all(experiments, models, clusters, patterns_data):
    """
    Validate all experiments. Returns list of error strings (empty = valid).

    Args:
        patterns_data: Dict from arrival-and-workload-patterns.yaml with
                      'arrival_patterns' and 'workloads' keys
    """
    errors = []
    valid_hw = {k for k in clusters if k != "namespace"}
    valid_harnesses = {"inference-perf", "orc"}

    # Extract arrival patterns and workloads from patterns_data
    arrival_patterns = patterns_data.get("arrival_patterns", {})
    workloads = patterns_data.get("workloads", {})

    for exp in experiments:
        eid = exp.get("id", "?")

        # Existing validations
        if exp["model"] not in models:
            errors.append(f"Experiment #{eid}: unknown model '{exp['model']}'")
        if exp["hw"] not in valid_hw:
            errors.append(f"Experiment #{eid}: unknown hw '{exp['hw']}'")

        # Validate workload exists
        if exp["workload"] not in workloads:
            errors.append(f"Experiment #{eid}: unknown workload '{exp['workload']}'")

        # Validate arrival_pattern exists
        if "arrival_pattern" not in exp:
            errors.append(f"Experiment #{eid}: missing 'arrival_pattern' field")
        elif exp["arrival_pattern"] not in arrival_patterns:
            errors.append(f"Experiment #{eid}: unknown arrival_pattern '{exp['arrival_pattern']}'")

        # Validate harness
        harness = exp.get("harness", "inference-perf")
        if harness not in valid_harnesses:
            errors.append(f"Experiment #{eid}: unknown harness '{harness}' (valid: {valid_harnesses})")

        # Validate combination is valid (workload has data for arrival_pattern)
        if exp["workload"] in workloads and "arrival_pattern" in exp:
            wl = workloads[exp["workload"]]
            if exp["arrival_pattern"] not in wl:
                available = ", ".join(wl.keys())
                errors.append(
                    f"Experiment #{eid}: workload '{exp['workload']}' does not have "
                    f"data for arrival_pattern '{exp['arrival_pattern']}'. "
                    f"Available: {available}"
                )

        # Dynamic workloads require ORC harness
        if harness not in ["orc", "blis-orc"]:
            errors.append(
                f"Experiment #{eid}: dynamically generated workloads require "
                f"harness='orc' or 'blis-orc', got harness='{harness}'"
            )

    return errors
```

- [ ] **Step 5: Update build_values_overrides function signature**

Change function signature (line 109) to accept patterns_file instead of workloads:

```python
# Old signature (line 109)
def build_values_overrides(exp, models, workloads):

# New signature (line 109)
def build_values_overrides(exp, models, patterns_file):
    """
    Build values.yaml overrides for an experiment.

    Args:
        exp: Experiment dict
        models: Models config dict
        patterns_file: Path to arrival-and-workload-patterns.yaml

    Returns:
        dict: Values overrides for this experiment
    """
```

- [ ] **Step 6: Update workload generation in build_values_overrides**

Replace static workload loading with dynamic generation (around line 200):

```python
# OLD CODE - REMOVE THIS SECTION (around lines 200-254)
    # Get workload data
    wl = workloads[exp["workload"]]
    spec_type = wl.get("spec", "inference_perf")

    if harness == "orc":
        # ORC harness: use appropriate format
        if spec_type == "inference_perf":
            # ... inference_perf format handling ...
        elif spec_type == "blis_native":
            # ... blis_native format handling ...
    else:
        # inference-perf harness (default)
        if spec_type == "inference_perf":
            # ... inference_perf format handling ...

# NEW CODE - ADD THIS SECTION
    # Generate workload dynamically
    wl = generate_workload_for_experiment(exp, patterns_file)

    # Dynamic workloads are always BLIS native format
    if harness in ["orc", "blis-orc"]:
        v["workload"]["orcSpec"] = wl

        # Calculate horizon from num_requests and aggregate_rate
        num_requests = wl.get("num_requests", 0)
        aggregate_rate = wl.get("aggregate_rate", 1.0)
        horizon = int(2 * num_requests / aggregate_rate) if aggregate_rate > 0 else 0
        v["workload"]["horizon"] = horizon
    else:
        raise ValueError(
            f"Dynamically generated workloads use BLIS native format "
            f"and require harness='orc' or 'blis-orc'. "
            f"Experiment #{exp['id']} has harness='{harness}'"
        )
```

- [ ] **Step 7: Update generate_campaign function**

Update the main generate_campaign function (around lines 397-400) to load patterns_data instead of workloads:

```python
# OLD CODE (lines 397-400)
    workloads = load_workloads(
        Path(__file__).parent.parent / "workloads.yaml"
    )

    # Validate experiments
    errors = validate_all(experiments, models, clusters, workloads)

# NEW CODE
    # Load arrival and workload patterns
    patterns_file = Path(__file__).parent / "arrival-and-workload-patterns.yaml"
    patterns_data = load_yaml(patterns_file)

    # Validate experiments
    errors = validate_all(experiments, models, clusters, patterns_data)
```

Update the call to build_values_overrides (around line 477):

```python
# OLD CODE (around line 477)
        v = build_values_overrides(exp, models, workloads)

# NEW CODE
        v = build_values_overrides(exp, models, patterns_file)
```

- [ ] **Step 8: Verify syntax**

Check that the modified file has valid Python syntax:

```bash
python3 -m py_compile blis-campaign/generate.py
```

Expected: No output (successful compilation)

- [ ] **Step 9: Commit the changes**

```bash
git add blis-campaign/generate.py
git commit -m "feat: use dynamic workload generation in campaign generator

Replace static workloads.yaml loading with dynamic workload generation
using combine_workload module. Import combine_workload and call it for
each experiment using workload + arrival_pattern fields.

Updates:
- Add generate_workload_for_experiment() function
- Update validate_all() to check arrival-and-workload-patterns.yaml
- Update build_values_overrides() to use dynamic generation
- Remove load_workloads() function
- Remove dependency on workloads.yaml

All experiments now generate workloads dynamically before pipeline
compilation.

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

## Task 3: Integration Testing

**Files:**
- Read: `blis-campaign/experiments.json`
- Read: Generated output files

- [ ] **Step 1: Test with small experiment subset**

Generate a test experiment to verify everything works:

```bash
# Create test experiments file with just one experiment
cat > /tmp/test-exp.json <<'EOF'
[
  {
    "id": 1,
    "model": "Llama-3.1-8B-Instruct",
    "workload": "m-mid",
    "hw": "H100",
    "tp": 1,
    "chunk_size": 2048,
    "gpu_mem": 0.9,
    "kv_offload": false,
    "dp": null,
    "scheduling": "priority",
    "arrival_pattern": "afternoon",
    "notes": "Test",
    "harness": "orc"
  }
]
EOF

python blis-campaign/generate.py --experiments /tmp/test-exp.json --output /tmp/test-campaign/
```

Expected: Script runs successfully, generates experiment #1

- [ ] **Step 2: Verify generated workload structure**

Check that the generated values.yaml contains the correct workload structure:

```bash
# Check that orcSpec exists in generated values
grep -A 5 "orcSpec:" /tmp/test-campaign/1/values.yaml
```

Expected: Should show BLIS native workload structure with version: '2', cohorts, etc.

- [ ] **Step 3: Verify cohorts were generated**

Check that cohorts contain the correct arrival pattern:

```bash
# Look for cohort IDs that include the arrival pattern
grep "id:" /tmp/test-campaign/1/values.yaml | grep "afternoon"
```

Expected: Should see cohort IDs like "afternoon-low", "afternoon-medium", "afternoon-high"

- [ ] **Step 4: Test with full experiments.json**

Generate all experiments:

```bash
python blis-campaign/generate.py --experiments blis-campaign/experiments.json --output /tmp/full-campaign/
```

Expected: Script runs successfully, generates all experiments without errors

- [ ] **Step 5: Verify different arrival patterns**

Check that different experiments have different arrival patterns:

```bash
# Check experiment 1 (afternoon)
grep "id.*afternoon" /tmp/full-campaign/1/values.yaml

# Check experiment 2 (morning)
grep "id.*morning" /tmp/full-campaign/2/values.yaml

# Check experiment 3 (midnight)
grep "id.*midnight" /tmp/full-campaign/3/values.yaml
```

Expected: Each experiment should have cohort IDs matching its arrival_pattern

- [ ] **Step 6: Test validation errors**

Test that validation catches invalid combinations:

```bash
# Create invalid experiment (non-existent arrival pattern)
cat > /tmp/invalid-exp.json <<'EOF'
[
  {
    "id": 999,
    "model": "Llama-3.1-8B-Instruct",
    "workload": "m-mid",
    "arrival_pattern": "invalid-pattern",
    "hw": "H100",
    "harness": "orc"
  }
]
EOF

python blis-campaign/generate.py --experiments /tmp/invalid-exp.json --output /tmp/invalid-campaign/ 2>&1 | grep "unknown arrival_pattern"
```

Expected: Should see error message: "Experiment #999: unknown arrival_pattern 'invalid-pattern'"

- [ ] **Step 7: Clean up test files**

```bash
rm -rf /tmp/test-campaign/ /tmp/full-campaign/ /tmp/invalid-campaign/
rm /tmp/test-exp.json /tmp/invalid-exp.json
```

Expected: Test directories removed

- [ ] **Step 8: Final verification commit**

No code changes, just verify everything works:

```bash
# Run a final generation test
python blis-campaign/generate.py --experiments blis-campaign/experiments.json --output /tmp/verify-campaign/
ls /tmp/verify-campaign/
rm -rf /tmp/verify-campaign/
```

Expected: All experiments generate successfully

---

## Self-Review Checklist

**Spec Coverage:**
- ✅ Refactor combine_workload.py to make output_file optional (Task 1, Steps 2-3)
- ✅ Return workload dict from combine_workload (Task 1, Step 4)
- ✅ Import combine_workload in generate.py (Task 2, Step 1)
- ✅ Add generate_workload_for_experiment function (Task 2, Step 3)
- ✅ Update validate_all to check arrival-and-workload-patterns.yaml (Task 2, Step 4)
- ✅ Update build_values_overrides to use dynamic generation (Task 2, Steps 5-6)
- ✅ Update generate_campaign to load patterns_data (Task 2, Step 7)
- ✅ Test with real experiments.json (Task 3)

**Placeholder Scan:**
- ✅ No TBD, TODO, or "implement later"
- ✅ All code blocks are complete and specific
- ✅ All commands have expected output
- ✅ No "add error handling" without specifics

**Type Consistency:**
- ✅ patterns_file used consistently (Path or str)
- ✅ patterns_data dict structure consistent (arrival_patterns, workloads keys)
- ✅ workload dict structure consistent (BLIS native format)
- ✅ Function signatures match across all tasks

**Task Completeness:**
- ✅ Task 1 has exact code for combine_workload.py changes
- ✅ Task 2 has exact code for generate.py changes
- ✅ Task 2 has verification commands
- ✅ Task 3 has comprehensive integration tests
- ✅ All tasks have commit steps

---

## Notes

- **Module Import:** Using sys.path.insert(0, ...) to import from blis-campaign directory. This works because generate.py is in blis-campaign/ and combine_workload.py is also in blis-campaign/.

- **BLIS Native Format:** Dynamic workloads always use BLIS native format (cohorts). This simplifies the generator logic - no need for spec_type branching.

- **ORC Harness Requirement:** Dynamic workloads require ORC harness. The validation catches this early with a clear error message.

- **Backward Compatibility:** This is a breaking change for experiments using inference-perf harness. All experiments in experiments.json already use "orc" or "blis-orc" harness, so no migration needed.

- **workloads.yaml:** After this change, workloads.yaml is no longer needed and can be removed or kept for reference.

- **Error Messages:** Include experiment ID in all error messages for easy debugging.
