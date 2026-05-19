# saturation_exps/tests/test_generate_campaign.py
import sys
sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
from pathlib import Path
import tempfile
import pytest
from generate_campaign import parse_args, find_workload_file


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


import yaml
import json
import subprocess
from unittest.mock import patch, MagicMock
from generate_campaign import (
    update_workload_trace_rate,
    load_json,
    load_yaml,
    write_yaml,
    generate_values_yaml,
    compile_pipeline,
    TEMPLATE_MAP,
    generate_pipelinerun,
    process_experiment
)


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

        # Mock compile_pipeline to avoid tektonc call
        with patch("generate_campaign.compile_pipeline") as mock_compile:
            # Mock side effect: create empty pipeline.yaml
            def create_pipeline(harness, exp_dir):
                (exp_dir / "pipeline.yaml").write_text("""
apiVersion: tekton.dev/v1
kind: Pipeline
metadata:
  name: blis-data-collection
spec:
  tasks: []
""")
            mock_compile.side_effect = create_pipeline

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
