# Saturation Campaign Generator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a tool to transform saturation point experiments into BLIS campaign-ready directories with pipeline YAML

**Architecture:** Single-file CLI script that reads saturation results, updates workload trace rates, generates values.yaml, and calls tektonc for pipeline compilation. Follows existing blis-campaign/generate.py patterns for consistency.

**Tech Stack:** Python 3.11+, PyYAML, argparse, subprocess (tektonc invocation)

---

## File Structure

**New files:**
- `saturation_exps/generate_campaign.py` - Main script with CLI, workload update, values generation, pipeline compilation
- `saturation_exps/tests/test_generate_campaign.py` - Unit tests for all components

**Modified files:**
- None (completely standalone tool)

**Config dependencies (read-only):**
- `blis-campaign/config/models.yaml` - Model configurations
- `blis-campaign/config/clusters.yaml` - Cluster configurations
- `tektoncsample/blis-orc/data_pipeline.yaml.j2` - ORC pipeline template
- `tektoncsample/blis-inference-perf/data_pipeline.yaml.j2` - inference-perf template

---

### Task 1: CLI Argument Parser

**Files:**
- Create: `saturation_exps/generate_campaign.py`
- Test: `saturation_exps/tests/test_generate_campaign.py`

- [ ] **Step 1: Write test for CLI parsing**

```python
# saturation_exps/tests/test_generate_campaign.py
import sys
sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
from generate_campaign import parse_args


def test_parse_args_with_experiments():
    """Test parsing --experiments flag with comma-separated list."""
    args = parse_args(["--experiments", "exp1,exp3,exp5"])
    assert args.experiments == ["exp1", "exp3", "exp5"]


def test_parse_args_missing_experiments():
    """Test that missing --experiments raises error."""
    try:
        parse_args([])
        assert False, "Should have raised SystemExit"
    except SystemExit:
        pass  # Expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest saturation_exps/tests/test_generate_campaign.py::test_parse_args_with_experiments -v`
Expected: `ModuleNotFoundError: No module named 'generate_campaign'`

- [ ] **Step 3: Write minimal CLI parser**

```python
# saturation_exps/generate_campaign.py
"""Generate BLIS campaign pipelines from saturation experiment folders.

Reads saturation_results.json, updates workload trace_rate to saturation point,
generates values.yaml, and compiles Tekton pipeline YAML.
"""
import argparse
from pathlib import Path


def parse_args(argv=None):
    """Parse command-line arguments.

    Args:
        argv: Optional argument list (for testing)

    Returns:
        argparse.Namespace with experiments list
    """
    parser = argparse.ArgumentParser(
        description="Generate BLIS campaign from saturation experiments"
    )
    parser.add_argument(
        "--experiments",
        required=True,
        help="Comma-separated list of experiment folders (e.g., exp1,exp3,exp5)"
    )
    args = parser.parse_args(argv)
    # Split comma-separated list into array
    args.experiments = [e.strip() for e in args.experiments.split(",")]
    return args


if __name__ == "__main__":
    args = parse_args()
    print(f"Processing experiments: {args.experiments}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest saturation_exps/tests/test_generate_campaign.py -v`
Expected: Both tests PASS

- [ ] **Step 5: Commit**

```bash
git add saturation_exps/generate_campaign.py saturation_exps/tests/test_generate_campaign.py
git commit -m "feat: add CLI argument parser for saturation campaign generator

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

### Task 2: Workload File Discovery

**Files:**
- Modify: `saturation_exps/generate_campaign.py`
- Modify: `saturation_exps/tests/test_generate_campaign.py`

- [ ] **Step 1: Write test for workload file discovery**

```python
# saturation_exps/tests/test_generate_campaign.py
from pathlib import Path
import tempfile
import pytest
from generate_campaign import find_workload_file


def test_find_workload_file_success():
    """Test finding exactly one workload YAML."""
    with tempfile.TemporaryDirectory() as tmpdir:
        exp_dir = Path(tmpdir)
        # Create the workload file
        (exp_dir / "saturation_mmid_afternoon.yaml").touch()
        # Create other files that should be ignored
        (exp_dir / "experiment.json").touch()
        (exp_dir / "saturation_results.json").touch()

        result = find_workload_file(exp_dir)
        assert result.name == "saturation_mmid_afternoon.yaml"


def test_find_workload_file_no_yaml():
    """Test error when no workload YAML found."""
    with tempfile.TemporaryDirectory() as tmpdir:
        exp_dir = Path(tmpdir)
        (exp_dir / "experiment.json").touch()

        with pytest.raises(FileNotFoundError, match="No workload YAML file found"):
            find_workload_file(exp_dir)


def test_find_workload_file_multiple_yamls():
    """Test error when multiple workload YAMLs found."""
    with tempfile.TemporaryDirectory() as tmpdir:
        exp_dir = Path(tmpdir)
        (exp_dir / "workload1.yaml").touch()
        (exp_dir / "workload2.yaml").touch()

        with pytest.raises(ValueError, match="Multiple workload files found"):
            find_workload_file(exp_dir)


def test_find_workload_file_excludes_generated():
    """Test that generated files are excluded from search."""
    with tempfile.TemporaryDirectory() as tmpdir:
        exp_dir = Path(tmpdir)
        (exp_dir / "saturation_mmid_afternoon.yaml").touch()
        (exp_dir / "values.yaml").touch()
        (exp_dir / "pipeline.yaml").touch()
        (exp_dir / "pipelinerun.yaml").touch()

        result = find_workload_file(exp_dir)
        assert result.name == "saturation_mmid_afternoon.yaml"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest saturation_exps/tests/test_generate_campaign.py::test_find_workload_file_success -v`
Expected: `AttributeError: module 'generate_campaign' has no attribute 'find_workload_file'`

- [ ] **Step 3: Implement workload file discovery**

```python
# saturation_exps/generate_campaign.py
# Add after parse_args function

def find_workload_file(exp_dir):
    """Find the workload YAML file in experiment directory.

    Args:
        exp_dir: Path to experiment directory

    Returns:
        Path to workload YAML file

    Raises:
        FileNotFoundError: If no workload YAML found
        ValueError: If multiple workload YAMLs found
    """
    # Exclude generated files
    exclude_files = {"values.yaml", "pipeline.yaml", "pipelinerun.yaml"}

    # Find all YAML files except excluded ones
    yaml_files = [
        f for f in exp_dir.glob("*.yaml")
        if f.name not in exclude_files
    ]

    if len(yaml_files) == 0:
        raise FileNotFoundError(
            f"No workload YAML file found in {exp_dir} "
            "(expected saturation_*.yaml or similar)"
        )

    if len(yaml_files) > 1:
        file_list = ", ".join(f.name for f in yaml_files)
        raise ValueError(
            f"Multiple workload files found in {exp_dir}: {file_list}. "
            "Expected exactly one YAML file."
        )

    return yaml_files[0]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest saturation_exps/tests/test_generate_campaign.py::test_find_workload -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add saturation_exps/generate_campaign.py saturation_exps/tests/test_generate_campaign.py
git commit -m "feat: add workload file discovery with exclusion logic

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

### Task 3: Workload Rate Updater

**Files:**
- Modify: `saturation_exps/generate_campaign.py`
- Modify: `saturation_exps/tests/test_generate_campaign.py`

- [ ] **Step 1: Write test for workload rate update**

```python
# saturation_exps/tests/test_generate_campaign.py
import yaml
from generate_campaign import update_workload_trace_rate


def test_update_workload_trace_rate():
    """Test updating all cohort trace_rate values."""
    workload_data = {
        "version": "2",
        "cohorts": [
            {"id": "cohort1", "spike": {"trace_rate": 1.0}},
            {"id": "cohort2", "spike": {"trace_rate": 2.0}},
            {"id": "cohort3", "spike": {"trace_rate": 3.0}}
        ],
        "aggregate_rate": 0
    }

    saturation_rps = 12.5
    updated = update_workload_trace_rate(workload_data, saturation_rps)

    assert len(updated["cohorts"]) == 3
    for cohort in updated["cohorts"]:
        assert cohort["spike"]["trace_rate"] == 12.5


def test_update_workload_trace_rate_missing_cohorts():
    """Test error when workload has no cohorts array."""
    workload_data = {"version": "2"}

    with pytest.raises(ValueError, match="Invalid workload: no cohorts array"):
        update_workload_trace_rate(workload_data, 10.0)


def test_update_workload_trace_rate_missing_spike():
    """Test error when cohort missing spike.trace_rate."""
    workload_data = {
        "cohorts": [
            {"id": "cohort1", "spike": {}},
        ]
    }

    with pytest.raises(ValueError, match="Cohort cohort1 missing spike.trace_rate"):
        update_workload_trace_rate(workload_data, 10.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest saturation_exps/tests/test_generate_campaign.py::test_update_workload_trace_rate -v`
Expected: `AttributeError: module 'generate_campaign' has no attribute 'update_workload_trace_rate'`

- [ ] **Step 3: Implement workload rate updater**

```python
# saturation_exps/generate_campaign.py
# Add after find_workload_file function

def update_workload_trace_rate(workload_data, saturation_rps):
    """Update all cohort trace_rate values to saturation point RPS.

    Args:
        workload_data: Parsed workload YAML dict
        saturation_rps: Saturation point RPS value

    Returns:
        Updated workload dict (modifies in-place and returns)

    Raises:
        ValueError: If workload structure is invalid
    """
    if "cohorts" not in workload_data:
        raise ValueError("Invalid workload: no cohorts array")

    for cohort in workload_data["cohorts"]:
        cohort_id = cohort.get("id", "unknown")

        if "spike" not in cohort:
            raise ValueError(f"Cohort {cohort_id} missing spike section")

        if "trace_rate" not in cohort["spike"]:
            raise ValueError(f"Cohort {cohort_id} missing spike.trace_rate field")

        # Update trace_rate to saturation point
        cohort["spike"]["trace_rate"] = saturation_rps

    return workload_data
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest saturation_exps/tests/test_generate_campaign.py::test_update_workload -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add saturation_exps/generate_campaign.py saturation_exps/tests/test_generate_campaign.py
git commit -m "feat: add workload trace_rate updater with validation

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

### Task 4: Config Loaders

**Files:**
- Modify: `saturation_exps/generate_campaign.py`
- Modify: `saturation_exps/tests/test_generate_campaign.py`

- [ ] **Step 1: Write test for config loading**

```python
# saturation_exps/tests/test_generate_campaign.py
from generate_campaign import load_json, load_yaml


def test_load_json():
    """Test JSON file loading."""
    with tempfile.TemporaryDirectory() as tmpdir:
        json_file = Path(tmpdir) / "test.json"
        json_file.write_text('{"key": "value", "number": 42}')

        data = load_json(json_file)
        assert data["key"] == "value"
        assert data["number"] == 42


def test_load_yaml():
    """Test YAML file loading."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_file = Path(tmpdir) / "test.yaml"
        yaml_file.write_text("key: value\nnumber: 42\n")

        data = load_yaml(yaml_file)
        assert data["key"] == "value"
        assert data["number"] == 42


def test_load_json_missing_file():
    """Test error on missing JSON file."""
    with pytest.raises(FileNotFoundError):
        load_json(Path("/nonexistent/file.json"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest saturation_exps/tests/test_generate_campaign.py::test_load_json -v`
Expected: `AttributeError: module 'generate_campaign' has no attribute 'load_json'`

- [ ] **Step 3: Implement config loaders**

```python
# saturation_exps/generate_campaign.py
# Add imports at top
import json
import yaml

# Add after update_workload_trace_rate function

def load_json(path):
    """Load JSON file.

    Args:
        path: Path to JSON file

    Returns:
        Parsed JSON data

    Raises:
        FileNotFoundError: If file doesn't exist
        json.JSONDecodeError: If JSON is invalid
    """
    with open(path) as f:
        return json.load(f)


def load_yaml(path):
    """Load YAML file.

    Args:
        path: Path to YAML file

    Returns:
        Parsed YAML data

    Raises:
        FileNotFoundError: If file doesn't exist
        yaml.YAMLError: If YAML is invalid
    """
    with open(path) as f:
        return yaml.safe_load(f)


def write_yaml(path, data):
    """Write data to YAML file.

    Args:
        path: Path to output YAML file
        data: Data to serialize
    """
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, width=200)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest saturation_exps/tests/test_generate_campaign.py::test_load -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add saturation_exps/generate_campaign.py saturation_exps/tests/test_generate_campaign.py
git commit -m "feat: add JSON and YAML config loaders

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

### Task 5: Values Generator

**Files:**
- Modify: `saturation_exps/generate_campaign.py`
- Modify: `saturation_exps/tests/test_generate_campaign.py`

- [ ] **Step 1: Write test for values generation**

```python
# saturation_exps/tests/test_generate_campaign.py
from generate_campaign import generate_values_yaml


def test_generate_values_yaml():
    """Test generating values.yaml structure."""
    experiment = {
        "id": 1,
        "model": "Llama-3.1-8B-Instruct",
        "hw": "H100",
        "tp": 1,
        "dp": None,
        "scheduling": "priority",
        "harness": "orc"
    }

    models = {
        "Llama-3.1-8B-Instruct": {
            "image": "vllm/vllm-openai:latest",
            "checkpoint": "meta-llama/Llama-3.1-8B-Instruct"
        }
    }

    clusters = {
        "H100": {
            "context": "gke_project_us-central1-a_cluster",
            "namespace": "blis"
        }
    }

    workload_file = Path("saturation_exps/exp1/workload.yaml")

    values = generate_values_yaml(experiment, models, clusters, workload_file)

    assert values["model"]["name"] == "Llama-3.1-8B-Instruct"
    assert values["model"]["image"] == "vllm/vllm-openai:latest"
    assert values["model"]["tp"] == 1
    assert values["cluster"]["context"] == "gke_project_us-central1-a_cluster"
    assert values["cluster"]["namespace"] == "blis"
    assert values["workload_file"] == str(workload_file)
    assert values["harness"] == "orc"


def test_generate_values_yaml_missing_model():
    """Test error when model not found in models.yaml."""
    experiment = {"model": "UnknownModel", "hw": "H100"}
    models = {}
    clusters = {"H100": {}}

    with pytest.raises(KeyError, match="Model UnknownModel not found"):
        generate_values_yaml(experiment, models, clusters, Path("w.yaml"))


def test_generate_values_yaml_missing_cluster():
    """Test error when cluster not found in clusters.yaml."""
    experiment = {"model": "TestModel", "hw": "UnknownHW"}
    models = {"TestModel": {"image": "test"}}
    clusters = {}

    with pytest.raises(KeyError, match="Hardware UnknownHW not found"):
        generate_values_yaml(experiment, models, clusters, Path("w.yaml"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest saturation_exps/tests/test_generate_campaign.py::test_generate_values_yaml -v`
Expected: `AttributeError: module 'generate_campaign' has no attribute 'generate_values_yaml'`

- [ ] **Step 3: Implement values generator**

```python
# saturation_exps/generate_campaign.py
# Add after write_yaml function

def generate_values_yaml(experiment, models, clusters, workload_file):
    """Generate values.yaml for tektonc compilation.

    Args:
        experiment: Experiment dict from experiment.json
        models: Models dict from models.yaml
        clusters: Clusters dict from clusters.yaml
        workload_file: Path to workload YAML file

    Returns:
        Values dict for YAML serialization

    Raises:
        KeyError: If model or hw not found in configs
    """
    model_name = experiment["model"]
    hw = experiment["hw"]

    # Validate model exists
    if model_name not in models:
        raise KeyError(f"Model {model_name} not found in models.yaml")

    # Validate hardware exists
    if hw not in clusters:
        raise KeyError(f"Hardware {hw} not found in clusters.yaml")

    model_config = models[model_name]
    cluster_config = clusters[hw]

    # Build values structure
    values = {
        "model": {
            "name": model_name,
            "image": model_config["image"],
            "checkpoint": model_config.get("checkpoint", model_name),
            "tp": experiment.get("tp", 1),
            "dp": experiment.get("dp", 1) if experiment.get("dp") else 1,
        },
        "cluster": {
            "context": cluster_config["context"],
            "namespace": cluster_config["namespace"],
        },
        "workload_file": str(workload_file),
        "harness": experiment.get("harness", "orc"),
        "scheduling": experiment.get("scheduling", "fcfs"),
    }

    # Add optional fields if present
    if "precision" in experiment:
        values["precision"] = experiment["precision"]
    if "gpu_mem" in experiment:
        values["gpu_mem"] = experiment["gpu_mem"]
    if "chunk_size" in experiment:
        values["chunk_size"] = experiment["chunk_size"]

    return values
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest saturation_exps/tests/test_generate_campaign.py::test_generate_values -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add saturation_exps/generate_campaign.py saturation_exps/tests/test_generate_campaign.py
git commit -m "feat: add values.yaml generator with model/cluster lookup

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

### Task 6: Pipeline Compiler

**Files:**
- Modify: `saturation_exps/generate_campaign.py`
- Modify: `saturation_exps/tests/test_generate_campaign.py`

- [ ] **Step 1: Write test for pipeline compilation**

```python
# saturation_exps/tests/test_generate_campaign.py
import subprocess
from unittest.mock import patch, MagicMock
from generate_campaign import compile_pipeline, TEMPLATE_MAP


def test_template_map():
    """Test harness to template mapping."""
    assert TEMPLATE_MAP["orc"] == "tektoncsample/blis-orc/data_pipeline.yaml.j2"
    assert TEMPLATE_MAP["blis-orc"] == "tektoncsample/blis-orc/data_pipeline.yaml.j2"
    assert TEMPLATE_MAP["inference-perf"] == "tektoncsample/blis-inference-perf/data_pipeline.yaml.j2"


@patch("subprocess.run")
def test_compile_pipeline_success(mock_run):
    """Test successful pipeline compilation."""
    mock_run.return_value = MagicMock(returncode=0, stderr="")

    with tempfile.TemporaryDirectory() as tmpdir:
        exp_dir = Path(tmpdir)
        values_file = exp_dir / "values.yaml"
        values_file.write_text("model: test\n")

        compile_pipeline("orc", exp_dir)

        # Verify subprocess.run was called with correct args
        assert mock_run.called
        call_args = mock_run.call_args[0][0]
        assert "python" in call_args[0]
        assert "tektonc.py" in call_args[1]
        assert "-t" in call_args
        assert "-f" in call_args
        assert "-o" in call_args


@patch("subprocess.run")
def test_compile_pipeline_failure(mock_run):
    """Test pipeline compilation failure."""
    mock_run.return_value = MagicMock(
        returncode=1,
        stderr="Template error: undefined variable"
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        exp_dir = Path(tmpdir)
        values_file = exp_dir / "values.yaml"
        values_file.write_text("model: test\n")

        with pytest.raises(RuntimeError, match="tektonc compilation failed"):
            compile_pipeline("orc", exp_dir)


def test_compile_pipeline_invalid_harness():
    """Test error for invalid harness."""
    with tempfile.TemporaryDirectory() as tmpdir:
        exp_dir = Path(tmpdir)

        with pytest.raises(ValueError, match="Unknown harness"):
            compile_pipeline("invalid-harness", exp_dir)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest saturation_exps/tests/test_generate_campaign.py::test_compile_pipeline -v`
Expected: `AttributeError: module 'generate_campaign' has no attribute 'compile_pipeline'`

- [ ] **Step 3: Implement pipeline compiler**

```python
# saturation_exps/generate_campaign.py
# Add import at top
import subprocess

# Add after generate_values_yaml function

# Template mapping
TEMPLATE_MAP = {
    "orc": "tektoncsample/blis-orc/data_pipeline.yaml.j2",
    "blis-orc": "tektoncsample/blis-orc/data_pipeline.yaml.j2",
    "inference-perf": "tektoncsample/blis-inference-perf/data_pipeline.yaml.j2",
}


def compile_pipeline(harness, exp_dir):
    """Compile Tekton pipeline YAML using tektonc.

    Args:
        harness: Harness type (orc, inference-perf)
        exp_dir: Path to experiment directory

    Raises:
        ValueError: If harness not recognized
        RuntimeError: If tektonc compilation fails
    """
    if harness not in TEMPLATE_MAP:
        raise ValueError(
            f"Unknown harness '{harness}'. "
            f"Valid options: {', '.join(TEMPLATE_MAP.keys())}"
        )

    template = TEMPLATE_MAP[harness]
    values_file = exp_dir / "values.yaml"
    output_file = exp_dir / "pipeline.yaml"

    # Call tektonc
    cmd = [
        "python",
        "tektonc/tektonc.py",
        "-t", template,
        "-f", str(values_file),
        "-o", str(output_file),
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"tektonc compilation failed (exit {result.returncode}):\n"
            f"{result.stderr}"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest saturation_exps/tests/test_generate_campaign.py::test_compile_pipeline -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add saturation_exps/generate_campaign.py saturation_exps/tests/test_generate_campaign.py
git commit -m "feat: add pipeline compiler with tektonc invocation

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

### Task 7: PipelineRun Generator

**Files:**
- Modify: `saturation_exps/generate_campaign.py`
- Modify: `saturation_exps/tests/test_generate_campaign.py`

- [ ] **Step 1: Write test for PipelineRun generation**

```python
# saturation_exps/tests/test_generate_campaign.py
from generate_campaign import generate_pipelinerun


def test_generate_pipelinerun():
    """Test PipelineRun YAML generation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        exp_dir = Path(tmpdir)
        pipeline_file = exp_dir / "pipeline.yaml"
        pipeline_file.write_text("""
apiVersion: tekton.dev/v1
kind: Pipeline
metadata:
  name: blis-data-collection
spec:
  tasks: []
""")

        generate_pipelinerun(exp_dir, exp_id=42)

        pipelinerun_file = exp_dir / "pipelinerun.yaml"
        assert pipelinerun_file.exists()

        pr_data = load_yaml(pipelinerun_file)
        assert pr_data["kind"] == "PipelineRun"
        assert "saturation-exp42" in pr_data["metadata"]["name"]
        assert pr_data["spec"]["pipelineRef"]["name"] == "blis-data-collection"


def test_generate_pipelinerun_missing_pipeline():
    """Test error when pipeline.yaml not found."""
    with tempfile.TemporaryDirectory() as tmpdir:
        exp_dir = Path(tmpdir)

        with pytest.raises(FileNotFoundError, match="pipeline.yaml not found"):
            generate_pipelinerun(exp_dir, exp_id=1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest saturation_exps/tests/test_generate_campaign.py::test_generate_pipelinerun -v`
Expected: `AttributeError: module 'generate_campaign' has no attribute 'generate_pipelinerun'`

- [ ] **Step 3: Implement PipelineRun generator**

```python
# saturation_exps/generate_campaign.py
# Add import at top
from datetime import datetime

# Add after compile_pipeline function

PIPELINERUN_TEMPLATE = """\
apiVersion: tekton.dev/v1
kind: PipelineRun
metadata:
  name: {name}
spec:
  timeouts:
    pipeline: 6h
    tasks: 5h30m
  taskRunTemplate:
    serviceAccountName: helm-installer
  pipelineRef:
    name: {pipeline_name}
  workspaces:
    - name: model-cache
      persistentVolumeClaim:
        claimName: model-pvc
    - name: data
      persistentVolumeClaim:
        claimName: data-pvc
    - name: hf-credentials
      secret:
        secretName: hf-secret
        items:
          - key: HF_TOKEN
            path: HF_TOKEN
    - name: target-credentials
      secret:
        secretName: s3-secret
        items:
          - key: ACCESS_KEY
            path: ACCESS_KEY
          - key: SECRET_KEY
            path: SECRET_KEY
"""


def generate_pipelinerun(exp_dir, exp_id):
    """Generate PipelineRun YAML for experiment.

    Args:
        exp_dir: Path to experiment directory
        exp_id: Experiment ID number

    Raises:
        FileNotFoundError: If pipeline.yaml not found
    """
    pipeline_file = exp_dir / "pipeline.yaml"
    if not pipeline_file.exists():
        raise FileNotFoundError(
            f"pipeline.yaml not found in {exp_dir}. "
            "Run compile_pipeline first."
        )

    # Read pipeline name from pipeline.yaml
    pipeline_data = load_yaml(pipeline_file)
    pipeline_name = pipeline_data["metadata"]["name"]

    # Generate unique PipelineRun name with timestamp
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    pr_name = f"saturation-exp{exp_id}-{timestamp}"

    # Generate PipelineRun YAML
    pr_yaml = PIPELINERUN_TEMPLATE.format(
        name=pr_name,
        pipeline_name=pipeline_name
    )

    # Write to file
    pipelinerun_file = exp_dir / "pipelinerun.yaml"
    pipelinerun_file.write_text(pr_yaml)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest saturation_exps/tests/test_generate_campaign.py::test_generate_pipelinerun -v`
Expected: Both tests PASS

- [ ] **Step 5: Commit**

```bash
git add saturation_exps/generate_campaign.py saturation_exps/tests/test_generate_campaign.py
git commit -m "feat: add PipelineRun YAML generator

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

### Task 8: Main Processing Loop

**Files:**
- Modify: `saturation_exps/generate_campaign.py`
- Modify: `saturation_exps/tests/test_generate_campaign.py`

- [ ] **Step 1: Write integration test for main loop**

```python
# saturation_exps/tests/test_generate_campaign.py
from generate_campaign import process_experiment


def test_process_experiment_end_to_end():
    """Test complete experiment processing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir)
        exp_dir = base_dir / "exp1"
        exp_dir.mkdir()

        # Create experiment.json
        experiment = {
            "id": 1,
            "model": "Llama-3.1-8B-Instruct",
            "hw": "H100",
            "tp": 1,
            "dp": None,
            "scheduling": "priority",
            "harness": "orc"
        }
        (exp_dir / "experiment.json").write_text(json.dumps(experiment))

        # Create saturation_results.json
        results = {
            "result": {
                "saturation_point_rps": 12.5
            }
        }
        (exp_dir / "saturation_results.json").write_text(json.dumps(results))

        # Create workload YAML
        workload = {
            "version": "2",
            "cohorts": [
                {"id": "c1", "spike": {"trace_rate": 1.0}},
                {"id": "c2", "spike": {"trace_rate": 2.0}}
            ]
        }
        workload_file = exp_dir / "saturation_test.yaml"
        write_yaml(workload_file, workload)

        # Mock configs
        models = {
            "Llama-3.1-8B-Instruct": {
                "image": "vllm/vllm-openai:latest",
                "checkpoint": "meta-llama/Llama-3.1-8B-Instruct"
            }
        }
        clusters = {
            "H100": {
                "context": "test-context",
                "namespace": "test-ns"
            }
        }

        # Mock tektonc call
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")

            # Process experiment
            success, error = process_experiment("exp1", base_dir, models, clusters)

            assert success is True
            assert error is None

            # Verify outputs
            assert (exp_dir / "values.yaml").exists()
            assert (exp_dir / "pipeline.yaml").exists()
            assert (exp_dir / "pipelinerun.yaml").exists()

            # Verify workload was updated
            updated_workload = load_yaml(workload_file)
            for cohort in updated_workload["cohorts"]:
                assert cohort["spike"]["trace_rate"] == 12.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest saturation_exps/tests/test_generate_campaign.py::test_process_experiment_end_to_end -v`
Expected: `AttributeError: module 'generate_campaign' has no attribute 'process_experiment'`

- [ ] **Step 3: Implement main processing function**

```python
# saturation_exps/generate_campaign.py
# Add after generate_pipelinerun function

def process_experiment(exp_name, base_dir, models, clusters):
    """Process a single saturation experiment.

    Args:
        exp_name: Experiment folder name (e.g., "exp1")
        base_dir: Base directory containing experiment folders
        models: Models config dict
        clusters: Clusters config dict

    Returns:
        Tuple of (success: bool, error: str or None)
    """
    exp_dir = base_dir / exp_name

    try:
        # 1. Load experiment config
        experiment = load_json(exp_dir / "experiment.json")

        # 2. Load saturation results
        sat_results = load_json(exp_dir / "saturation_results.json")
        saturation_rps = sat_results["result"]["saturation_point_rps"]

        # 3. Find and load workload file
        workload_file = find_workload_file(exp_dir)
        workload_data = load_yaml(workload_file)

        # 4. Update workload trace_rate
        updated_workload = update_workload_trace_rate(workload_data, saturation_rps)
        write_yaml(workload_file, updated_workload)

        # 5. Generate values.yaml
        values = generate_values_yaml(experiment, models, clusters, workload_file)
        write_yaml(exp_dir / "values.yaml", values)

        # 6. Compile pipeline
        harness = experiment.get("harness", "orc")
        compile_pipeline(harness, exp_dir)

        # 7. Generate PipelineRun
        generate_pipelinerun(exp_dir, experiment["id"])

        return True, None

    except FileNotFoundError as e:
        return False, f"Missing file: {e}"
    except KeyError as e:
        return False, f"Missing required field: {e}"
    except ValueError as e:
        return False, f"Validation error: {e}"
    except RuntimeError as e:
        return False, f"Compilation error: {e}"
    except Exception as e:
        return False, f"Unexpected error: {e}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest saturation_exps/tests/test_generate_campaign.py::test_process_experiment_end_to_end -v`
Expected: Test PASS

- [ ] **Step 5: Commit**

```bash
git add saturation_exps/generate_campaign.py saturation_exps/tests/test_generate_campaign.py
git commit -m "feat: add main experiment processing loop

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

### Task 9: Main Entry Point

**Files:**
- Modify: `saturation_exps/generate_campaign.py`

- [ ] **Step 1: Implement main function**

```python
# saturation_exps/generate_campaign.py
# Add after process_experiment function

def main():
    """Main entry point."""
    args = parse_args()

    # Determine base directory
    script_dir = Path(__file__).parent
    base_dir = script_dir  # saturation_exps/

    # Load shared configs
    config_dir = script_dir.parent / "blis-campaign" / "config"

    try:
        models = load_yaml(config_dir / "models.yaml")
        clusters = load_yaml(config_dir / "clusters.yaml")
    except FileNotFoundError as e:
        print(f"ERROR: Config file not found: {e}")
        return 1

    # Process each experiment
    results = []
    for exp_name in args.experiments:
        print(f"\nProcessing {exp_name}...")

        # Check if experiment folder exists
        exp_dir = base_dir / exp_name
        if not exp_dir.is_dir():
            print(f"  ERROR: Experiment folder not found: {exp_dir}")
            results.append((exp_name, False, "Folder not found"))
            continue

        # Process experiment
        success, error = process_experiment(exp_name, base_dir, models, clusters)
        results.append((exp_name, success, error))

        if success:
            print(f"  ✓ Generated pipeline files in {exp_dir}")
        else:
            print(f"  ✗ Failed: {error}")

    # Print summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    succeeded = [r for r in results if r[1]]
    failed = [r for r in results if not r[1]]

    print(f"Processed {len(results)} experiments: "
          f"{len(succeeded)} succeeded, {len(failed)} failed")

    if failed:
        print("\nFailed experiments:")
        for exp_name, _, error in failed:
            print(f"  - {exp_name}: {error}")

    # Exit code: 0 if any succeeded, 1 if all failed
    return 0 if succeeded else 1


# Update __main__ block
if __name__ == "__main__":
    exit(main())
```

- [ ] **Step 2: Test main function manually**

Run: `python saturation_exps/generate_campaign.py --experiments exp1`
Expected: Process exp1 and show results

- [ ] **Step 3: Commit**

```bash
git add saturation_exps/generate_campaign.py
git commit -m "feat: add main entry point with summary reporting

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

### Task 10: Add Usage Documentation

**Files:**
- Create: `saturation_exps/README.md`

- [ ] **Step 1: Write README**

```markdown
# Saturation Experiment Campaign Generator

Generate BLIS campaign pipelines from saturation point experiments.

## Usage

```bash
python saturation_exps/generate_campaign.py --experiments exp1,exp3,exp5
```

This will:
1. Read `saturation_point_rps` from each experiment's `saturation_results.json`
2. Update all cohort `trace_rate` values in the workload YAML
3. Generate `values.yaml`, `pipeline.yaml`, and `pipelinerun.yaml`
4. Write all outputs to the experiment folder

## Input Requirements

Each experiment folder must contain:
- `experiment.json` - Model/hardware config with optional `harness` field
- `saturation_results.json` - Must have `result.saturation_point_rps`
- `saturation_*.yaml` - BLIS-native workload (exactly one YAML file)

## Running the Campaign

After generation, use the existing campaign runner:

```bash
# Run specific experiments
./blis-campaign/run-campaign.sh \
  --campaign saturation_exps/ \
  --hw H100 \
  --only 1,3,5

# Run a range of experiments
./blis-campaign/run-campaign.sh \
  --campaign saturation_exps/ \
  --hw H100 \
  --range 1-10
```

## Example

```bash
# Generate pipelines
python saturation_exps/generate_campaign.py --experiments exp1,exp2

# Output:
# Processing exp1...
#   ✓ Generated pipeline files in saturation_exps/exp1
# Processing exp2...
#   ✓ Generated pipeline files in saturation_exps/exp2
#
# SUMMARY
# Processed 2 experiments: 2 succeeded, 0 failed

# Run campaign
./blis-campaign/run-campaign.sh --campaign saturation_exps/ --hw H100 --only 1,2
```

## Troubleshooting

**Error: "No workload YAML file found"**
- Ensure experiment folder contains exactly one `.yaml` file (excluding values.yaml, pipeline.yaml, pipelinerun.yaml)

**Error: "Model X not found in models.yaml"**
- Check that the model name in `experiment.json` matches an entry in `blis-campaign/config/models.yaml`

**Error: "Hardware X not found in clusters.yaml"**
- Check that the hw field in `experiment.json` matches an entry in `blis-campaign/config/clusters.yaml`

**Error: "tektonc compilation failed"**
- Check that the harness field in `experiment.json` is valid ("orc", "blis-orc", or "inference-perf")
- Verify the template file exists in `tektoncsample/`
```

- [ ] **Step 2: Commit**

```bash
git add saturation_exps/README.md
git commit -m "docs: add saturation campaign generator README

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

### Task 11: Run Full Integration Test

**Files:**
- None (manual testing)

- [ ] **Step 1: Test with real exp1 data**

Run: `python saturation_exps/generate_campaign.py --experiments exp1`
Expected: Successfully generate all files

- [ ] **Step 2: Verify generated files**

```bash
ls saturation_exps/exp1/
```
Expected: See `values.yaml`, `pipeline.yaml`, `pipelinerun.yaml`

- [ ] **Step 3: Check workload was updated**

```bash
grep trace_rate saturation_exps/exp1/saturation_mmid_afternoon.yaml | head -3
```
Expected: All trace_rate values should be `12.406919642857135` (the saturation point)

- [ ] **Step 4: Validate pipeline YAML**

```bash
python tektonc/tektonc.py --explain \
  -t tektoncsample/blis-orc/data_pipeline.yaml.j2 \
  -f saturation_exps/exp1/values.yaml
```
Expected: Show task list without errors

- [ ] **Step 5: Test error handling**

Run: `python saturation_exps/generate_campaign.py --experiments exp1,exp999`
Expected: Process exp1 successfully, show error for exp999, exit 0 (partial success)

- [ ] **Step 6: Document test results**

If all tests pass, ready for deployment. If any fail, fix issues and re-test.

---

## Self-Review Checklist

**Spec Coverage:**
- ✓ CLI argument parser (Task 1)
- ✓ Workload file discovery with exclusion (Task 2)
- ✓ Workload trace_rate updater for all cohorts (Task 3)
- ✓ JSON/YAML config loaders (Task 4)
- ✓ Values.yaml generator (Task 5)
- ✓ Pipeline compiler with tektonc (Task 6)
- ✓ PipelineRun generator (Task 7)
- ✓ Main processing loop with error handling (Task 8)
- ✓ Summary reporting and exit codes (Task 9)
- ✓ Documentation (Task 10)
- ✓ Integration testing (Task 11)

**Placeholder Check:**
- No TBD, TODO, or "implement later" statements
- All code blocks contain complete implementations
- All error messages are specific and actionable
- No vague "add validation" or "handle errors" steps

**Type Consistency:**
- All functions use consistent parameter names (`exp_dir`, `workload_data`, etc.)
- Return types are consistent (tuples, dicts, paths)
- YAML structure matches spec (spike.trace_rate, cohorts array)

**Missing Requirements:**
None found. All spec requirements have corresponding tasks.
