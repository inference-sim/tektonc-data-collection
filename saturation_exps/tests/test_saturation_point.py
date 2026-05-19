import pytest
import tempfile
import json
import os
from pathlib import Path
import yaml

def test_experiment_loader_finds_directory():
    """Test that ExperimentLoader resolves experiment directory correctly."""
    # This will fail because ExperimentLoader doesn't exist yet
    from find_saturation_point import ExperimentLoader

    loader = ExperimentLoader("exp1", base_dir="saturation_exps")
    assert loader.exp_dir == Path("saturation_exps/exp1")

def test_experiment_loader_loads_config():
    """Test loading experiment.json with server configuration."""
    from find_saturation_point import ExperimentLoader

    # Create temporary experiment directory structure
    with tempfile.TemporaryDirectory() as tmpdir:
        exp_dir = Path(tmpdir) / "exp1"
        exp_dir.mkdir()

        # Write test experiment.json
        exp_config = {
            "id": 1,
            "model": "Llama-3.1-8B-Instruct",
            "hw": "H100",
            "tp": 1,
            "chunk_size": 2048,
            "gpu_mem": 0.9,
            "scheduling": "priority",
            "precision": "BF16",
            "mbt": 2048,
            "max_model_len": 8192
        }
        with open(exp_dir / "experiment.json", "w") as f:
            json.dump(exp_config, f)

        loader = ExperimentLoader("exp1", base_dir=tmpdir)
        config = loader.load_server_config()

        assert config["model"] == "Llama-3.1-8B-Instruct"
        assert config["tp"] == 1
        assert config["hw"] == "H100"

def test_experiment_loader_autodetects_yaml():
    """Test auto-detection of workload YAML file."""
    from find_saturation_point import ExperimentLoader

    with tempfile.TemporaryDirectory() as tmpdir:
        exp_dir = Path(tmpdir) / "exp1"
        exp_dir.mkdir()

        # Create experiment.json
        with open(exp_dir / "experiment.json", "w") as f:
            json.dump({"id": 1, "model": "test"}, f)

        # Create workload YAML
        workload = {
            "version": "2",
            "cohorts": [
                {
                    "id": "test-cohort",
                    "spike": {"trace_rate": 100.0}
                }
            ]
        }
        with open(exp_dir / "test_workload.yaml", "w") as f:
            yaml.dump(workload, f)

        loader = ExperimentLoader("exp1", base_dir=tmpdir)
        workload_spec, baseline_rate = loader.load_workload_spec()

        assert workload_spec["cohorts"][0]["id"] == "test-cohort"
        assert baseline_rate == 100.0

def test_experiment_loader_validates_uniform_rates():
    """Test validation that all cohorts have same trace rate."""
    from find_saturation_point import ExperimentLoader

    with tempfile.TemporaryDirectory() as tmpdir:
        exp_dir = Path(tmpdir) / "exp1"
        exp_dir.mkdir()

        with open(exp_dir / "experiment.json", "w") as f:
            json.dump({"id": 1, "model": "test"}, f)

        # Create workload with non-uniform rates
        workload = {
            "version": "2",
            "cohorts": [
                {"id": "cohort1", "spike": {"trace_rate": 100.0}},
                {"id": "cohort2", "spike": {"trace_rate": 150.0}}  # Different!
            ]
        }
        with open(exp_dir / "test_workload.yaml", "w") as f:
            yaml.dump(workload, f)

        loader = ExperimentLoader("exp1", base_dir=tmpdir)

        with pytest.raises(ValueError, match="All cohorts must have same trace_rate"):
            loader.load_workload_spec()

def test_workload_spec_generator_modifies_rate():
    """Test generating modified workload YAML with new trace rate."""
    from find_saturation_point import WorkloadSpecGenerator

    base_workload = {
        "version": "2",
        "cohorts": [
            {"id": "cohort1", "spike": {"trace_rate": 100.0, "duration_us": 600000000}},
            {"id": "cohort2", "spike": {"trace_rate": 100.0, "duration_us": 600000000}}
        ]
    }

    generator = WorkloadSpecGenerator(base_workload)
    temp_path = generator.generate(target_rate=150.0)

    # Verify file was created
    assert temp_path.exists()

    # Load and verify modified content
    with open(temp_path) as f:
        modified = yaml.safe_load(f)

    assert modified["cohorts"][0]["spike"]["trace_rate"] == 150.0
    assert modified["cohorts"][1]["spike"]["trace_rate"] == 150.0
    # Duration should be unchanged
    assert modified["cohorts"][0]["spike"]["duration_us"] == 600000000

    # Cleanup
    temp_path.unlink()

def test_blis_runner_constructs_command():
    """Test BLIS command construction from server config."""
    from find_saturation_point import BLISRunner

    server_config = {
        "model": "Llama-3.1-8B-Instruct",
        "hw": "H100",
        "tp": 1,
        "chunk_size": 2048,
        "gpu_mem": 0.9,
        "scheduling": "priority",
        "precision": "BF16",
        "mbt": 2048,
        "max_model_len": 8192
    }

    runner = BLISRunner(
        blis_binary="inference-sim/blis",
        server_config=server_config
    )

    workload_path = Path("/tmp/test_workload.yaml")
    output_path = Path("/tmp/test_output.json")

    cmd = runner._build_command(workload_path, output_path)

    assert "inference-sim/blis" in cmd
    assert "run" in cmd
    assert "--latency-model" in cmd
    assert "trained-physics" in cmd
    assert "--post-hoc-detector" in cmd
    assert "composite" in cmd
    assert "--model" in cmd
    assert "Llama-3.1-8B-Instruct" in cmd

def test_blis_runner_parses_verdict():
    """Test parsing saturation verdict from BLIS output."""
    from find_saturation_point import BLISRunner

    runner = BLISRunner(
        blis_binary="inference-sim/blis",
        server_config={"model": "test"}
    )

    # Create mock output JSON
    with tempfile.TemporaryDirectory() as tmpdir:
        output_path = Path(tmpdir) / "output.json"
        mock_output = {
            "saturation": {
                "level": "OVERLOADED",
                "score": 0.487,
                "confidence": 0.92,
                "signals": {
                    "rate_deficit": 0.023,
                    "latency_trend": 0.487,
                    "quartile_monotone": 1.0,
                    "noise_floor": 0.071
                }
            }
        }
        with open(output_path, "w") as f:
            json.dump(mock_output, f)

        verdict, saturation = runner._parse_verdict(output_path)

        assert verdict == "OVERLOADED"
        assert saturation["score"] == 0.487
        assert saturation["signals"]["latency_trend"] == 0.487

def test_saturation_searcher_coarse_multipliers():
    """Test coarse search multiplier generation."""
    from find_saturation_point import SaturationSearcher

    searcher = SaturationSearcher(baseline_rate=100.0, coarse_step=0.5)
    multipliers = searcher._generate_coarse_multipliers()

    # Should start with 1.0, then alternate up/down
    assert multipliers[0] == 1.0
    assert multipliers[1] == 1.5  # up
    assert multipliers[2] == 0.5  # down
    assert multipliers[3] == 2.0  # up
    assert multipliers[4] == 0.25  # down

def test_saturation_searcher_coarse_search_brackets():
    """Test coarse search brackets saturation point."""
    from find_saturation_point import SaturationSearcher

    searcher = SaturationSearcher(baseline_rate=100.0)

    # Mock BLIS runner function
    def mock_run_blis(rate: float) -> tuple[str, dict, dict]:
        verdict = "STABLE" if rate < 150.0 else "OVERLOADED"
        return verdict, {}, {}

    stable_rates, overloaded_rates = searcher._coarse_search(mock_run_blis)

    assert len(stable_rates) > 0
    assert len(overloaded_rates) > 0
    assert max(stable_rates) < min(overloaded_rates)

def test_saturation_searcher_fine_search():
    """Test fine binary search converges."""
    from find_saturation_point import SaturationSearcher

    searcher = SaturationSearcher(baseline_rate=100.0)

    # Mock BLIS runner function
    def mock_run_blis(rate: float) -> tuple[str, dict, dict]:
        verdict = "STABLE" if rate < 150.0 else "OVERLOADED"
        return verdict, {}, {}

    saturation_point = searcher._fine_search(
        stable_rate=100.0,
        overloaded_rate=200.0,
        run_blis_fn=mock_run_blis,
        precision=5.0
    )

    # Should converge to boundary near 150
    assert 145.0 <= saturation_point <= 155.0

def test_result_reporter_console_output(capsys):
    """Test console output formatting."""
    from find_saturation_point import ResultReporter

    all_results = [
        {"rate_rps": 100.0, "verdict": "STABLE", "phase": "coarse", "multiplier": 1.0},
        {"rate_rps": 200.0, "verdict": "OVERLOADED", "phase": "coarse", "multiplier": 2.0},
        {"rate_rps": 150.0, "verdict": "STABLE", "phase": "fine", "multiplier": 1.5},
    ]

    reporter = ResultReporter(
        exp_id="exp1",
        baseline_rate=100.0,
        saturation_point=150.0,
        all_results=all_results
    )

    reporter.print_console()

    captured = capsys.readouterr()
    assert "SATURATION POINT: 150.00 RPS" in captured.out
    assert "exp1" in captured.out

def test_result_reporter_json_output():
    """Test JSON output structure."""
    from find_saturation_point import ResultReporter

    all_results = [
        {"rate_rps": 100.0, "verdict": "STABLE", "phase": "coarse", "multiplier": 1.0},
        {"rate_rps": 200.0, "verdict": "OVERLOADED", "phase": "coarse", "multiplier": 2.0},
    ]

    reporter = ResultReporter(
        exp_id="exp1",
        baseline_rate=100.0,
        saturation_point=150.0,
        all_results=all_results
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        output_path = Path(tmpdir) / "results.json"
        reporter.save_json(
            output_path=output_path,
            config={"model": "test"},
            search_params={"precision_rps": 5.0}
        )

        with open(output_path) as f:
            data = json.load(f)

        assert data["experiment_id"] == "exp1"
        assert data["result"]["saturation_point_rps"] == 150.0
        assert len(data["all_runs"]) == 2
