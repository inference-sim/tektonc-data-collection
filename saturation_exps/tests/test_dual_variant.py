import sys
sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
import pytest
from pathlib import Path
from generate_campaign import extract_variant_rates


def test_extract_variant_rates():
    """Test rate calculation for saturation and overloaded variants."""
    # From exp1 saturation results
    saturation_results = {
        "result": {
            "saturation_point_rps": 12.406919642857135,
            "final_precision_rps": 0.5639508928571413
        }
    }

    saturation_rate, overloaded_rate = extract_variant_rates(saturation_results)

    assert saturation_rate == 12.406919642857135
    # Overloaded = saturation + precision
    assert overloaded_rate == pytest.approx(12.970870535714277)


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

        if not success:
            pytest.fail(f"generate_variant failed: {error}")
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
