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
