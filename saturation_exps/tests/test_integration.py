# saturation_exps/tests/test_integration.py
"""Integration test for full dual-variant generation pipeline."""
import subprocess
import shutil
from pathlib import Path


def test_full_generation_pipeline():
    """Test complete generation pipeline with exp1."""
    # Run in saturation_exps directory to use real config/templates
    script_dir = Path(__file__).parent.parent

    # Run generation script
    result = subprocess.run(
        ["python", "generate_campaign.py", "--experiments", "exp1"],
        cwd=script_dir,
        capture_output=True,
        text=True
    )

    # Verify success
    assert result.returncode == 0, f"Generation failed:\n{result.stderr}\n{result.stdout}"

    # Verify both variants created
    assert (script_dir / "exp1_saturation").is_dir()
    assert (script_dir / "exp1_overloaded").is_dir()

    try:
        # Verify complete file sets for saturation variant
        sat_dir = script_dir / "exp1_saturation"
        assert (sat_dir / "experiment.json").exists()
        assert (sat_dir / "workload_saturation.yaml").exists()
        assert (sat_dir / "values.yaml").exists()
        assert (sat_dir / "pipeline.yaml").exists()
        assert (sat_dir / "pipelinerun.yaml").exists()

        # Verify complete file sets for overloaded variant
        over_dir = script_dir / "exp1_overloaded"
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

    finally:
        # Cleanup generated directories
        import shutil as sh
        if (script_dir / "exp1_saturation").exists():
            sh.rmtree(script_dir / "exp1_saturation")
        if (script_dir / "exp1_overloaded").exists():
            sh.rmtree(script_dir / "exp1_overloaded")
