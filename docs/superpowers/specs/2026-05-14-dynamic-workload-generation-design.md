# Design: Dynamic Workload Generation for BLIS Campaign

**Date:** 2026-05-14
**Status:** Approved
**Author:** Claude Code

## Problem Statement

The current blis-campaign generator loads workloads from a static `workloads.yaml` file. However, experiments.json already contains both `workload` and `arrival_pattern` fields, and there's an existing `combine_workload.py` script that can generate complete workload specifications by combining these two dimensions.

The static approach requires maintaining a pre-generated workloads.yaml file, which becomes stale and doesn't leverage the composability provided by the arrival-and-workload-patterns.yaml structure.

## Solution

Modify the generator to dynamically generate workloads on-demand by:
1. Refactoring `combine_workload.py` to be importable as a module
2. Importing and calling it from `generate.py` for each experiment
3. Removing dependency on the static `workloads.yaml` file

All generation happens locally before cluster deployment, so there's no performance concern with in-process module calls.

## Design

### Architecture

**Current Flow:**
```
experiments.json → generate.py → load workloads.yaml → use static workload
```

**New Flow:**
```
experiments.json (workload + arrival_pattern)
    ↓
generate.py
    ↓
combine_workload(workload, arrival_pattern)
    ↓
reads arrival-and-workload-patterns.yaml
    ↓
returns workload dict
    ↓
use dynamic workload
```

### File Changes

**1. `blis-campaign/combine_workload.py`**

Make the `combine_workload()` function usable as both a module and CLI tool:

**Changes:**
- Make `output_file` parameter optional (default: None)
- Return the workload dict in addition to writing file
- Skip file writing when `output_file=None`
- Keep all validation and combination logic unchanged

**Signature:**
```python
def combine_workload(patterns_file, workload_name, arrival_pattern, output_file=None, seed=42):
    """
    Combine arrival patterns and workload distributions into a BLIS workload.

    Args:
        patterns_file: Path to arrival-and-workload-patterns.yaml
        workload_name: Name of the workload (e.g., "m-mid")
        arrival_pattern: Name of the arrival pattern (e.g., "morning")
        output_file: Path to output file (optional, skip file writing if None)
        seed: Random seed for the workload (default: 42)

    Returns:
        dict: BLIS workload structure
    """
```

**Return value:**
```python
{
    'version': '2',
    'seed': 42,
    'category': '',
    'clients': [],
    'cohorts': [...],
    'aggregate_rate': 0
}
```

**2. `blis-campaign/generate.py`**

**Add import:**
```python
import sys
# Add parent directory to path to import from blis-campaign
sys.path.insert(0, str(Path(__file__).parent))
from combine_workload import combine_workload
```

**Remove:**
- `load_workloads()` function
- Call to `load_workloads()` in `generate_campaign()`
- `workloads` parameter throughout

**Add new function:**
```python
def generate_workload_for_experiment(exp, patterns_file):
    """
    Generate workload dynamically for an experiment.

    Args:
        exp: Experiment dict with 'workload' and 'arrival_pattern' fields
        patterns_file: Path to arrival-and-workload-patterns.yaml

    Returns:
        dict: BLIS native workload structure
    """
    workload_name = exp["workload"]
    arrival_pattern = exp["arrival_pattern"]

    # Call combine_workload without output_file to get in-memory result
    workload_data = combine_workload(
        patterns_file=patterns_file,
        workload_name=workload_name,
        arrival_pattern=arrival_pattern,
        output_file=None,  # Don't write to file
        seed=42
    )

    return workload_data
```

**Update `validate_all()`:**

Replace workloads.yaml validation with arrival-and-workload-patterns.yaml validation:

**Old validation:**
```python
def validate_all(experiments, models, clusters, workloads):
    ...
    if exp["workload"] not in workloads:
        errors.append(f"Experiment #{eid}: unknown workload '{exp['workload']}'")
```

**New validation:**
```python
def validate_all(experiments, models, clusters, patterns_data):
    """
    Validate all experiments. Returns list of error strings (empty = valid).

    Args:
        patterns_data: Dict from arrival-and-workload-patterns.yaml with
                      'arrival_patterns' and 'workloads' keys
    """
    errors = []
    arrival_patterns = patterns_data.get("arrival_patterns", {})
    workloads = patterns_data.get("workloads", {})

    for exp in experiments:
        eid = exp.get("id", "?")

        # Validate workload exists
        if exp["workload"] not in workloads:
            errors.append(f"Experiment #{eid}: unknown workload '{exp['workload']}'")

        # Validate arrival_pattern exists
        if "arrival_pattern" not in exp:
            errors.append(f"Experiment #{eid}: missing 'arrival_pattern' field")
        elif exp["arrival_pattern"] not in arrival_patterns:
            errors.append(f"Experiment #{eid}: unknown arrival_pattern '{exp['arrival_pattern']}'")

        # Validate combination is valid
        if exp["workload"] in workloads and "arrival_pattern" in exp:
            wl = workloads[exp["workload"]]
            if exp["arrival_pattern"] not in wl:
                available = ", ".join(wl.keys())
                errors.append(
                    f"Experiment #{eid}: workload '{exp['workload']}' does not have "
                    f"data for arrival_pattern '{exp['arrival_pattern']}'. "
                    f"Available: {available}"
                )
```

**Update `build_values_overrides()`:**

Replace static workload loading with dynamic generation:

**Old code:**
```python
def build_values_overrides(exp, models, workloads):
    ...
    # Get workload data
    wl = workloads[exp["workload"]]
    spec_type = wl.get("spec", "inference_perf")
```

**New code:**
```python
def build_values_overrides(exp, models, patterns_file):
    """
    Build values.yaml overrides for an experiment.

    Args:
        exp: Experiment dict
        models: Models config dict
        patterns_file: Path to arrival-and-workload-patterns.yaml
    """
    ...
    # Generate workload dynamically
    wl = generate_workload_for_experiment(exp, patterns_file)

    # BLIS native format is always generated by combine_workload
    spec_type = "blis_native"
```

**Update `generate_campaign()`:**

Replace workloads.yaml loading with arrival-and-workload-patterns.yaml:

**Old code:**
```python
workloads = load_workloads(
    Path(__file__).parent.parent / "workloads.yaml"
)
errors = validate_all(experiments, models, clusters, workloads)
```

**New code:**
```python
patterns_file = Path(__file__).parent / "arrival-and-workload-patterns.yaml"
patterns_data = load_yaml(patterns_file)

errors = validate_all(experiments, models, clusters, patterns_data)

# ... later in experiment loop ...
v = build_values_overrides(exp, models, patterns_file)
```

### Workload Format Handling

**Key Insight:** The `combine_workload.py` script always generates BLIS native format (version 2 with cohorts). This simplifies the generator logic:

**Current generator has two paths:**
1. `spec_type = "inference_perf"` → convert to orcSpec or profileTemplate
2. `spec_type = "blis_native"` → use directly

**New generator:**
- Dynamic workloads are always BLIS native (from combine_workload)
- For ORC harness: use workload directly as `orcSpec`
- For inference-perf harness: Not supported (BLIS native workloads require ORC)

**Updated workload population logic:**

```python
# For ORC harness
if harness == "orc":
    v["workload"]["orcSpec"] = wl  # wl is already BLIS native

    # Calculate horizon from num_requests and aggregate_rate
    num_requests = wl.get("num_requests", 0)
    aggregate_rate = wl.get("aggregate_rate", 1.0)
    horizon = int(2 * num_requests / aggregate_rate) if aggregate_rate > 0 else 0
    v["workload"]["horizon"] = horizon
else:
    # inference-perf harness
    raise ValueError(
        f"Dynamically generated workloads use BLIS native format "
        f"and require harness='orc'. Experiment #{exp['id']} has harness='{harness}'"
    )
```

### Files NOT Modified

- `experiments.json` - Already has required `workload` and `arrival_pattern` fields
- `arrival-and-workload-patterns.yaml` - Source data remains unchanged
- Pipeline templates (data_pipeline.yaml.j2) - No changes to Tekton structure
- Values files - No changes to base values

### Files That Become Optional

- `workloads.yaml` - No longer needed, can be removed or kept for reference

## Validation Strategy

### Pre-generation Validation

In `validate_all()`:
1. Check `workload` field exists in arrival-and-workload-patterns.yaml workloads section
2. Check `arrival_pattern` field exists in arrival-and-workload-patterns.yaml arrival_patterns section
3. Check the combination is valid (workload has data for the specified arrival_pattern)
4. Check harness is "orc" (dynamic workloads require ORC harness)

### Generation-time Validation

In `generate_workload_for_experiment()`:
- combine_workload will raise ValueError if combination is invalid
- Catch and re-raise with experiment ID for better error messages

### Error Handling

```python
def generate_workload_for_experiment(exp, patterns_file):
    try:
        workload_data = combine_workload(
            patterns_file=patterns_file,
            workload_name=exp["workload"],
            arrival_pattern=exp["arrival_pattern"],
            output_file=None,
            seed=42
        )
        return workload_data
    except ValueError as e:
        raise ValueError(f"Experiment #{exp['id']}: {e}")
```

## Testing Strategy

1. **Module import test:**
   - Verify combine_workload can be imported
   - Verify it returns correct dict structure

2. **Validation test:**
   - Test with invalid workload name
   - Test with invalid arrival_pattern
   - Test with valid but incompatible combination

3. **Generation test:**
   - Generate a complete experiment
   - Verify workload structure matches BLIS native format
   - Verify horizon calculation is correct

4. **Integration test:**
   - Run generate.py on real experiments.json
   - Verify all experiments generate successfully
   - Compare output to previous static workload approach

## Backward Compatibility

**Breaking Change:** Experiments that used inference-perf harness with dynamically generated workloads will fail with a clear error message.

**Migration Path:**
1. All experiments in experiments.json should use `"harness": "orc"` or `"harness": "blis-orc"`
2. The `workload` and `arrival_pattern` fields must exist for all experiments
3. The combinations must be valid in arrival-and-workload-patterns.yaml

**Existing experiments.json:** Already has both required fields and uses ORC harness, so no migration needed.

## Rationale

### Why Module Import vs. Subprocess?

- All generation happens locally before cluster deployment
- No performance concern with in-process calls
- Simpler error handling and data passing
- No subprocess overhead or file I/O

### Why Make output_file Optional?

- Preserves standalone CLI tool functionality
- Enables in-memory usage for generate.py
- Single source of truth for combination logic

### Why Remove workloads.yaml?

- Reduces maintenance burden (one less file to keep in sync)
- Leverages composability of arrival-and-workload-patterns.yaml
- Makes workload generation deterministic from experiments.json

### Why BLIS Native Only?

- combine_workload.py generates BLIS native format (cohorts)
- Simplifies generator logic (no spec_type branching)
- ORC harness is the primary target for these experiments

## Implementation Plan

1. Refactor `combine_workload.py`:
   - Make output_file optional
   - Return workload dict

2. Update `generate.py`:
   - Add import
   - Add generate_workload_for_experiment()
   - Update validate_all()
   - Update build_values_overrides()
   - Update generate_campaign()

3. Test with real experiments.json

4. Update documentation

## References

- **combine_workload.py:** `blis-campaign/combine_workload.py` (combination logic)
- **arrival-and-workload-patterns.yaml:** `blis-campaign/arrival-and-workload-patterns.yaml` (source data)
- **experiments.json:** `blis-campaign/experiments.json` (already has required fields)
- **generate.py:** `blis-campaign/generate.py` (current generator)
