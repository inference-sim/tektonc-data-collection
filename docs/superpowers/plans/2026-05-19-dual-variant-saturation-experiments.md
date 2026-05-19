# Dual-Variant Saturation Experiments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend saturation_exps/generate_campaign.py to generate two experiment variants (saturation and overloaded) for each input experiment.

**Architecture:** Refactor process_experiment() to generate two output directories with different trace_rate values. Saturation variant uses saturation_point_rps, overloaded variant uses saturation_point_rps + final_precision_rps. Both variants are independent experiments with separate workload files.

**Tech Stack:** Python 3.12, YAML, subprocess (tektonc), existing generate_campaign.py infrastructure

---

## File Structure

**Modified files:**
- `saturation_exps/generate_campaign.py` - Add variant generation logic
- `saturation_exps/README.md` - Update usage documentation

**Test files:**
- `saturation_exps/tests/test_dual_variant.py` - New test file for variant generation

**Output structure** (generated, not tracked):
- `saturation_exps/{exp}_saturation/` - Saturation variant directory
- `saturation_exps/{exp}_overloaded/` - Overloaded variant directory

---

### Task 1: Add Rate Calculation Helper

**Files:**
- Modify: `saturation_exps/generate_campaign.py:456-463`
- Create: `saturation_exps/tests/test_dual_variant.py`

- [ ] **Step 1: Write failing test for rate extraction**

Create test file:

```python
# saturation_exps/tests/test_dual_variant.py
"""Tests for dual-variant generation logic."""
import pytest
from pathlib import Path


def test_extract_variant_rates():
    """Test extracting saturation and overloaded rates from saturation_results.json."""
    from generate_campaign import extract_variant_rates

    sat_results = {
        "result": {
            "saturation_point_rps": 12.406919642857135,
            "final_precision_rps": 0.5639508928571413
        }
    }

    saturation_rate, overloaded_rate = extract_variant_rates(sat_results)

    assert saturation_rate == 12.406919642857135
    assert overloaded_rate == pytest.approx(12.970870535714276, rel=1e-9)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd saturation_exps && python -m pytest tests/test_dual_variant.py::test_extract_variant_rates -v`
Expected: FAIL with "cannot import name 'extract_variant_rates'"

- [ ] **Step 3: Implement extract_variant_rates function**

Add function after `update_workload_trace_rate()` in generate_campaign.py:

```python
def extract_variant_rates(sat_results):
    """Extract saturation and overloaded rates from saturation_results.json.

    Args:
        sat_results: Parsed saturation_results.json dict

    Returns:
        Tuple of (saturation_rate, overloaded_rate)

    Raises:
        KeyError: If required fields missing
    """
    result = sat_results["result"]
    saturation_rps = result["saturation_point_rps"]
    final_precision_rps = result["final_precision_rps"]

    overloaded_rps = saturation_rps + final_precision_rps

    return saturation_rps, overloaded_rps
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd saturation_exps && python -m pytest tests/test_dual_variant.py::test_extract_variant_rates -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add saturation_exps/generate_campaign.py saturation_exps/tests/test_dual_variant.py
git commit -m "feat(saturation): add rate extraction for dual variants

Add extract_variant_rates() to calculate saturation and overloaded rates
from saturation_results.json. Overloaded rate = saturation + precision.

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

### Task 2: Add Variant Workload Generation

**Files:**
- Modify: `saturation_exps/generate_campaign.py` (after extract_variant_rates)
- Modify: `saturation_exps/tests/test_dual_variant.py`

- [ ] **Step 1: Write failing test for variant workload generation**

Add to test_dual_variant.py:

```python
def test_create_variant_workload():
    """Test creating workload file with specific variant rate."""
    from generate_campaign import create_variant_workload
    import tempfile
    import yaml

    # Sample workload structure
    workload_data = {
        "cohorts": [
            {"id": "C1", "spike": {"trace_rate": 10.0}},
            {"id": "C2", "spike": {"trace_rate": 10.0}}
        ]
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        output_path = Path(tmpdir) / "workload_test.yaml"

        create_variant_workload(workload_data, 15.5, output_path)

        # Verify file created
        assert output_path.exists()

        # Verify trace_rate updated
        result = yaml.safe_load(output_path.read_text())
        assert result["cohorts"][0]["spike"]["trace_rate"] == 15.5
        assert result["cohorts"][1]["spike"]["trace_rate"] == 15.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd saturation_exps && python -m pytest tests/test_dual_variant.py::test_create_variant_workload -v`
Expected: FAIL with "cannot import name 'create_variant_workload'"

- [ ] **Step 3: Implement create_variant_workload function**

Add function after extract_variant_rates():

```python
def create_variant_workload(workload_data, variant_rate, output_path):
    """Create workload file with specific trace_rate for a variant.

    Args:
        workload_data: Original workload dict
        variant_rate: RPS value for this variant
        output_path: Path where variant workload should be written

    Returns:
        Updated workload dict
    """
    import copy

    # Deep copy to avoid modifying original
    variant_workload = copy.deepcopy(workload_data)

    # Update all cohort trace_rate values
    updated = update_workload_trace_rate(variant_workload, variant_rate)

    # Write to output path
    write_yaml(output_path, updated)

    return updated
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd saturation_exps && python -m pytest tests/test_dual_variant.py::test_create_variant_workload -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add saturation_exps/generate_campaign.py saturation_exps/tests/test_dual_variant.py
git commit -m "feat(saturation): add variant workload generation

Add create_variant_workload() to generate workload files with
variant-specific trace_rate values.

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

### Task 3: Add Single Variant Generation

**Files:**
- Modify: `saturation_exps/generate_campaign.py` (after create_variant_workload)
- Modify: `saturation_exps/tests/test_dual_variant.py`

- [ ] **Step 1: Write failing test for variant directory generation**

Add to test_dual_variant.py:

```python
def test_generate_variant():
    """Test generating a complete variant directory."""
    from generate_campaign import generate_variant, load_json, load_yaml
    import tempfile
    import shutil

    # Use exp1 as test data source
    exp1_dir = Path(__file__).parent.parent / "exp1"

    with tempfile.TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir)

        # Copy exp1 source to temp dir
        src_dir = base_dir / "exp1"
        shutil.copytree(exp1_dir, src_dir)

        # Load test data
        experiment = load_json(src_dir / "experiment.json")
        sat_results = load_json(src_dir / "saturation_results.json")

        # Mock clusters and base_values_path (minimal for test)
        clusters = {"H100": {"gpu_label_value": "nvidia.com/gpu.product=H100"}}
        base_values_path = Path(__file__).parent.parent.parent / "tektoncsample" / "blis-orc" / "values.yaml"

        # Generate saturation variant
        output_dir = base_dir / "exp1_saturation"
        variant_rate = 12.407

        success, error = generate_variant(
            exp_name="exp1",
            variant_name="saturation",
            variant_rate=variant_rate,
            source_dir=src_dir,
            output_dir=output_dir,
            experiment=experiment,
            clusters=clusters,
            base_values_path=base_values_path
        )

        assert success is True
        assert error is None

        # Verify output structure
        assert (output_dir / "experiment.json").exists()
        assert (output_dir / "workload_saturation.yaml").exists()
        assert (output_dir / "values.yaml").exists()
        assert (output_dir / "pipeline.yaml").exists()
        assert (output_dir / "pipelinerun.yaml").exists()

        # Verify experiment.json has updated id
        variant_exp = load_json(output_dir / "experiment.json")
        assert variant_exp["id"] == "exp1_saturation"

        # Verify workload has correct rate
        workload = load_yaml(output_dir / "workload_saturation.yaml")
        assert workload["cohorts"][0]["spike"]["trace_rate"] == variant_rate
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd saturation_exps && python -m pytest tests/test_dual_variant.py::test_generate_variant -v`
Expected: FAIL with "cannot import name 'generate_variant'"

- [ ] **Step 3: Implement generate_variant function**

Add function after create_variant_workload():

```python
def generate_variant(exp_name, variant_name, variant_rate, source_dir,
                     output_dir, experiment, clusters, base_values_path):
    """Generate a complete variant directory with all pipeline files.

    Args:
        exp_name: Base experiment name (e.g., "exp1")
        variant_name: Variant suffix (e.g., "saturation" or "overloaded")
        variant_rate: RPS value for this variant
        source_dir: Source experiment directory (contains original workload)
        output_dir: Output directory for variant
        experiment: Parsed experiment.json dict
        clusters: Clusters config dict
        base_values_path: Path to base values template

    Returns:
        Tuple of (success: bool, error: str or None)
    """
    try:
        # Create output directory
        output_dir.mkdir(parents=True, exist_ok=True)

        # 1. Copy and update experiment.json
        variant_experiment = experiment.copy()
        variant_experiment["id"] = f"{exp_name}_{variant_name}"
        write_json(output_dir / "experiment.json", variant_experiment)

        # 2. Find source workload file
        workload_file = find_workload_file(source_dir)
        workload_data = load_yaml(workload_file)

        # 3. Create variant workload with updated trace_rate
        variant_workload_path = output_dir / f"workload_{variant_name}.yaml"
        create_variant_workload(workload_data, variant_rate, variant_workload_path)

        # 4. Generate values.yaml
        variant_workload_data = load_yaml(variant_workload_path)
        values = generate_values_yaml(
            variant_experiment, clusters, variant_workload_path,
            variant_workload_data, base_values_path
        )
        write_yaml(output_dir / "values.yaml", values)

        # 5. Compile pipeline.yaml
        harness = variant_experiment.get("harness", "orc")
        compile_pipeline(harness, output_dir)

        # 6. Generate pipelinerun.yaml
        generate_pipelinerun(output_dir, variant_experiment["id"])

        return True, None

    except Exception as e:
        return False, str(e)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd saturation_exps && python -m pytest tests/test_dual_variant.py::test_generate_variant -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add saturation_exps/generate_campaign.py saturation_exps/tests/test_dual_variant.py
git commit -m "feat(saturation): add single variant generation

Add generate_variant() to create complete variant directory with
experiment.json, workload, values.yaml, and compiled pipeline files.

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

### Task 4: Refactor process_experiment for Dual Variants

**Files:**
- Modify: `saturation_exps/generate_campaign.py:436-491` (process_experiment function)

- [ ] **Step 1: Write failing test for dual variant generation**

Add to test_dual_variant.py:

```python
def test_process_experiment_dual_variants():
    """Test that process_experiment generates both variants."""
    from generate_campaign import process_experiment
    import tempfile
    import shutil

    # Use exp1 as test data
    exp1_dir = Path(__file__).parent.parent / "exp1"

    with tempfile.TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir)

        # Copy exp1 to temp dir
        src_dir = base_dir / "exp1"
        shutil.copytree(exp1_dir, src_dir)

        # Mock configs
        clusters = {"H100": {"gpu_label_value": "nvidia.com/gpu.product=H100"}}
        base_values_path = Path(__file__).parent.parent.parent / "tektoncsample" / "blis-orc" / "values.yaml"

        # Process experiment
        success, error = process_experiment("exp1", base_dir, clusters, base_values_path)

        assert success is True
        assert error is None

        # Verify both variants exist
        assert (base_dir / "exp1_saturation").is_dir()
        assert (base_dir / "exp1_overloaded").is_dir()

        # Verify saturation variant files
        assert (base_dir / "exp1_saturation" / "experiment.json").exists()
        assert (base_dir / "exp1_saturation" / "workload_saturation.yaml").exists()
        assert (base_dir / "exp1_saturation" / "values.yaml").exists()

        # Verify overloaded variant files
        assert (base_dir / "exp1_overloaded" / "experiment.json").exists()
        assert (base_dir / "exp1_overloaded" / "workload_overloaded.yaml").exists()
        assert (base_dir / "exp1_overloaded" / "values.yaml").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd saturation_exps && python -m pytest tests/test_dual_variant.py::test_process_experiment_dual_variants -v`
Expected: FAIL (variants not generated yet)

- [ ] **Step 3: Refactor process_experiment to generate both variants**

Replace process_experiment function (lines 436-491):

```python
def process_experiment(exp_name, base_dir, clusters, base_values_path):
    """Process a single saturation experiment - generates both variants.

    Args:
        exp_name: Experiment folder name (e.g., "exp1")
        base_dir: Base directory containing experiment folders
        clusters: Clusters config dict
        base_values_path: Path to base values template

    Returns:
        Tuple of (success: bool, error: str or None)
    """
    source_dir = base_dir / exp_name

    try:
        # 1. Load source experiment config
        experiment = load_json(source_dir / "experiment.json")

        # 2. Load saturation results and extract rates
        sat_results = load_json(source_dir / "saturation_results.json")
        saturation_rate, overloaded_rate = extract_variant_rates(sat_results)

        # 3. Generate saturation variant
        saturation_dir = base_dir / f"{exp_name}_saturation"
        success_sat, error_sat = generate_variant(
            exp_name=exp_name,
            variant_name="saturation",
            variant_rate=saturation_rate,
            source_dir=source_dir,
            output_dir=saturation_dir,
            experiment=experiment,
            clusters=clusters,
            base_values_path=base_values_path
        )

        if not success_sat:
            return False, f"Saturation variant failed: {error_sat}"

        # 4. Generate overloaded variant
        overloaded_dir = base_dir / f"{exp_name}_overloaded"
        success_over, error_over = generate_variant(
            exp_name=exp_name,
            variant_name="overloaded",
            variant_rate=overloaded_rate,
            source_dir=source_dir,
            output_dir=overloaded_dir,
            experiment=experiment,
            clusters=clusters,
            base_values_path=base_values_path
        )

        if not success_over:
            return False, f"Overloaded variant failed: {error_over}"

        return True, None

    except FileNotFoundError as e:
        return False, f"Missing file: {e}"
    except KeyError as e:
        return False, f"Missing required field: {e}"
    except ValueError as e:
        return False, f"Validation error: {e}"
    except Exception as e:
        return False, f"Unexpected error: {e}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd saturation_exps && python -m pytest tests/test_dual_variant.py::test_process_experiment_dual_variants -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add saturation_exps/generate_campaign.py saturation_exps/tests/test_dual_variant.py
git commit -m "refactor(saturation): generate both variants in process_experiment

Refactor process_experiment() to generate two independent variant
directories (saturation and overloaded) instead of modifying source.

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

### Task 5: Update Main Output Logging

**Files:**
- Modify: `saturation_exps/generate_campaign.py:527-548` (main function output)

- [ ] **Step 1: Update success logging for dual variants**

Modify the success output in main() function (around line 528-530):

Find:
```python
        if success:
            print(f"  ✓ Generated pipeline files in {exp_dir}")
        else:
            print(f"  ✗ Failed: {error}")
```

Replace with:
```python
        if success:
            print(f"  ✓ Generated {exp_name}_saturation (rate: {sat_results['result']['saturation_point_rps']:.3f} RPS - saturation point)")
            print(f"  ✓ Generated {exp_name}_overloaded (rate: {sat_results['result']['saturation_point_rps'] + sat_results['result']['final_precision_rps']:.3f} RPS - saturation + precision)")
        else:
            print(f"  ✗ Failed: {error}")
```

But this requires loading sat_results in main. Better approach - return rates from process_experiment.

- [ ] **Step 2: Refactor process_experiment to return rates**

Update process_experiment signature and return:

```python
def process_experiment(exp_name, base_dir, clusters, base_values_path):
    """Process a single saturation experiment - generates both variants.

    Args:
        exp_name: Experiment folder name (e.g., "exp1")
        base_dir: Base directory containing experiment folders
        clusters: Clusters config dict
        base_values_path: Path to base values template

    Returns:
        Tuple of (success: bool, error: str or None, saturation_rate: float or None, overloaded_rate: float or None)
    """
    source_dir = base_dir / exp_name

    try:
        # 1. Load source experiment config
        experiment = load_json(source_dir / "experiment.json")

        # 2. Load saturation results and extract rates
        sat_results = load_json(source_dir / "saturation_results.json")
        saturation_rate, overloaded_rate = extract_variant_rates(sat_results)

        # 3. Generate saturation variant
        saturation_dir = base_dir / f"{exp_name}_saturation"
        success_sat, error_sat = generate_variant(
            exp_name=exp_name,
            variant_name="saturation",
            variant_rate=saturation_rate,
            source_dir=source_dir,
            output_dir=saturation_dir,
            experiment=experiment,
            clusters=clusters,
            base_values_path=base_values_path
        )

        if not success_sat:
            return False, f"Saturation variant failed: {error_sat}", None, None

        # 4. Generate overloaded variant
        overloaded_dir = base_dir / f"{exp_name}_overloaded"
        success_over, error_over = generate_variant(
            exp_name=exp_name,
            variant_name="overloaded",
            variant_rate=overloaded_rate,
            source_dir=source_dir,
            output_dir=overloaded_dir,
            experiment=experiment,
            clusters=clusters,
            base_values_path=base_values_path
        )

        if not success_over:
            return False, f"Overloaded variant failed: {error_over}", None, None

        return True, None, saturation_rate, overloaded_rate

    except FileNotFoundError as e:
        return False, f"Missing file: {e}", None, None
    except KeyError as e:
        return False, f"Missing required field: {e}", None, None
    except ValueError as e:
        return False, f"Validation error: {e}", None, None
    except Exception as e:
        return False, f"Unexpected error: {e}", None, None
```

- [ ] **Step 3: Update main() to use returned rates**

Update main() function call and output (around lines 527-530):

```python
        # Process experiment
        success, error, sat_rate, over_rate = process_experiment(exp_name, base_dir, clusters, base_values_path)
        results.append((exp_name, success, error))

        if success:
            print(f"  ✓ Generated {exp_name}_saturation (rate: {sat_rate:.3f} RPS - saturation point)")
            print(f"  ✓ Generated {exp_name}_overloaded (rate: {over_rate:.3f} RPS - saturation + precision)")
        else:
            print(f"  ✗ Failed: {error}")
```

- [ ] **Step 4: Test manually with exp1**

Run: `cd saturation_exps && python generate_campaign.py --experiments exp1`
Expected output:
```
Processing exp1...
  ✓ Generated exp1_saturation (rate: 12.407 RPS - saturation point)
  ✓ Generated exp1_overloaded (rate: 12.971 RPS - saturation + precision)

============================================================
SUMMARY
============================================================
Processed 1 experiments: 1 succeeded, 0 failed
```

- [ ] **Step 5: Verify generated directories**

Run: `ls -la saturation_exps/exp1_*`
Expected: Both exp1_saturation/ and exp1_overloaded/ directories exist with complete file sets

- [ ] **Step 6: Commit**

```bash
git add saturation_exps/generate_campaign.py
git commit -m "feat(saturation): update output logging for dual variants

Update main() to show both generated variants with their respective
rates. Makes it clear which variant is saturation vs overloaded.

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

### Task 6: Update Documentation

**Files:**
- Modify: `saturation_exps/README.md`

- [ ] **Step 1: Update usage section with dual variant output**

Find the "This will:" section (lines 11-15) and replace:

```markdown
This will:
1. Read `saturation_point_rps` and `final_precision_rps` from each experiment's `saturation_results.json`
2. Calculate two rates:
   - Saturation rate: `saturation_point_rps`
   - Overloaded rate: `saturation_point_rps + final_precision_rps`
3. Generate two variant directories per experiment:
   - `{exp}_saturation/` - Contains workload at saturation point
   - `{exp}_overloaded/` - Contains workload at overloaded rate
4. Each variant includes: `experiment.json`, `workload_{variant}.yaml`, `values.yaml`, `pipeline.yaml`, and `pipelinerun.yaml`
5. Both variants enable composite post-hoc detector for saturation analysis
```

- [ ] **Step 2: Update example section**

Replace example output (lines 50-52) with:

```markdown
# Output:
# Processing exp1...
#   ✓ Generated exp1_saturation (rate: 12.407 RPS - saturation point)
#   ✓ Generated exp1_overloaded (rate: 12.971 RPS - saturation + precision)
# Processing exp2...
#   ✓ Generated exp2_saturation (rate: X.XXX RPS - saturation point)
#   ✓ Generated exp2_overloaded (rate: Y.YYY RPS - saturation + precision)
#
# SUMMARY
# Processed 2 experiments: 2 succeeded, 0 failed
```

- [ ] **Step 3: Update running section**

Replace running examples (lines 30-42) with:

```bash
# Run both variants of specific experiments
./blis-campaign/run-campaign.sh \
  --campaign saturation_exps/ \
  --hw H100 \
  --only exp1_saturation,exp1_overloaded,exp2_saturation,exp2_overloaded

# Run only saturation variants
./blis-campaign/run-campaign.sh \
  --campaign saturation_exps/ \
  --hw H100 \
  --only exp1_saturation,exp2_saturation,exp3_saturation

# Run only overloaded variants
./blis-campaign/run-campaign.sh \
  --campaign saturation_exps/ \
  --hw H100 \
  --only exp1_overloaded,exp2_overloaded,exp3_overloaded

# Run all variants
./blis-campaign/run-campaign.sh \
  --campaign saturation_exps/ \
  --hw H100
```

- [ ] **Step 4: Add section explaining variant purpose**

Add new section after "Usage" and before "Input Requirements":

```markdown
## Variants

Each saturation experiment generates two variants:

**Saturation variant** (`{exp}_saturation/`):
- Runs at the discovered `saturation_point_rps` (highest stable rate)
- Expected detector result: `STABLE` classification
- Validates that the saturation search correctly identified stable operation

**Overloaded variant** (`{exp}_overloaded/`):
- Runs at `saturation_point_rps + final_precision_rps` (slightly above threshold)
- Expected detector result: `OVERLOADED` or `BACKLOGGED` classification
- Confirms system behavior degrades above saturation point

Both variants:
- Deploy independent model instances (no interference)
- Use composite post-hoc detector for saturation analysis
- Output results to separate directories with complete observe data
```

- [ ] **Step 5: Commit**

```bash
git add saturation_exps/README.md
git commit -m "docs(saturation): update README for dual variants

Document dual-variant generation: saturation and overloaded.
Update usage examples, explain variant purpose, show expected
detector results for each variant.

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

### Task 7: Integration Test

**Files:**
- Create: `saturation_exps/tests/test_integration.py`

- [ ] **Step 1: Write integration test**

```python
# saturation_exps/tests/test_integration.py
"""Integration test for full dual-variant generation pipeline."""
import subprocess
import shutil
from pathlib import Path


def test_full_generation_pipeline():
    """Test complete generation pipeline with exp1."""
    import tempfile

    # Use exp1 as test source
    exp1_dir = Path(__file__).parent.parent / "exp1"

    with tempfile.TemporaryDirectory() as tmpdir:
        test_base = Path(tmpdir)

        # Copy exp1 source to test directory
        test_exp1 = test_base / "exp1"
        shutil.copytree(exp1_dir, test_exp1)

        # Copy generate_campaign.py to test directory (to run in isolation)
        script_path = Path(__file__).parent.parent / "generate_campaign.py"
        test_script = test_base / "generate_campaign.py"
        shutil.copy(script_path, test_script)

        # Copy required config files
        config_src = Path(__file__).parent.parent.parent / "blis-campaign" / "config"
        config_dst = test_base / "blis-campaign" / "config"
        config_dst.mkdir(parents=True)
        shutil.copy(config_src / "clusters.yaml", config_dst / "clusters.yaml")

        # Copy template directory
        template_src = Path(__file__).parent.parent.parent / "tektoncsample"
        template_dst = test_base / "tektoncsample"
        shutil.copytree(template_src, template_dst)

        # Run generation script
        result = subprocess.run(
            ["python", str(test_script), "--experiments", "exp1"],
            cwd=test_base,
            capture_output=True,
            text=True
        )

        # Verify success
        assert result.returncode == 0, f"Generation failed:\n{result.stderr}"

        # Verify both variants created
        assert (test_base / "exp1_saturation").is_dir()
        assert (test_base / "exp1_overloaded").is_dir()

        # Verify complete file sets for saturation variant
        sat_dir = test_base / "exp1_saturation"
        assert (sat_dir / "experiment.json").exists()
        assert (sat_dir / "workload_saturation.yaml").exists()
        assert (sat_dir / "values.yaml").exists()
        assert (sat_dir / "pipeline.yaml").exists()
        assert (sat_dir / "pipelinerun.yaml").exists()

        # Verify complete file sets for overloaded variant
        over_dir = test_base / "exp1_overloaded"
        assert (over_dir / "experiment.json").exists()
        assert (over_dir / "workload_overloaded.yaml").exists()
        assert (over_dir / "values.yaml").exists()
        assert (over_dir / "pipeline.yaml").exists()
        assert (over_dir / "pipelinerun.yaml").exists()

        # Verify output shows both variants
        assert "exp1_saturation" in result.stdout
        assert "exp1_overloaded" in result.stdout
        assert "12.407 RPS" in result.stdout  # saturation rate
        assert "12.971 RPS" in result.stdout  # overloaded rate
```

- [ ] **Step 2: Run integration test**

Run: `cd saturation_exps && python -m pytest tests/test_integration.py::test_full_generation_pipeline -v -s`
Expected: PASS with complete generation output

- [ ] **Step 3: Commit**

```bash
git add saturation_exps/tests/test_integration.py
git commit -m "test(saturation): add integration test for dual variants

End-to-end test that validates complete dual-variant generation
pipeline including file creation, compilation, and output logging.

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

## Self-Review Checklist

**Spec coverage:**
- ✅ Rate calculation (saturation + precision) - Task 1
- ✅ Separate workload files - Task 2
- ✅ Independent variant directories - Task 3
- ✅ Dual variant generation - Task 4
- ✅ Output logging - Task 5
- ✅ Documentation - Task 6
- ✅ Integration testing - Task 7

**Placeholder scan:**
- ✅ No TBD, TODO, or placeholders
- ✅ Complete code in every step
- ✅ Exact file paths and commands

**Type consistency:**
- ✅ extract_variant_rates returns tuple(float, float)
- ✅ create_variant_workload takes Path for output_path
- ✅ generate_variant returns tuple(bool, str or None)
- ✅ process_experiment returns tuple(bool, str or None, float or None, float or None)
- ✅ All function signatures consistent across tasks

**Testing:**
- ✅ Unit tests for each new function
- ✅ Integration test for complete pipeline
- ✅ Test-driven development (write test first, then implement)
