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
