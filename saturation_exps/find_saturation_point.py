#!/usr/bin/env python3
"""Find saturation point for LLM serving workloads using BLIS composite detector."""

import argparse
import copy
import json
import os
import subprocess
import sys
import tempfile
import yaml
from datetime import datetime, UTC
from pathlib import Path


class ExperimentLoader:
    """Load and validate experiment configuration."""

    def __init__(self, exp_id: str, base_dir: str = "."):
        self.exp_id = exp_id
        self.base_dir = Path(base_dir)
        self.exp_dir = self.base_dir / exp_id

    def load_server_config(self) -> dict:
        """Load server configuration from experiment.json."""
        config_path = self.exp_dir / "experiment.json"
        if not config_path.exists():
            raise FileNotFoundError(f"experiment.json not found in {self.exp_dir}")

        with open(config_path) as f:
            return json.load(f)

    def load_workload_spec(self) -> tuple[dict, float]:
        """Load workload spec and extract baseline trace rate.

        Returns:
            (workload_spec dict, baseline_rate float)
        """
        # Auto-detect first .yaml file
        yaml_files = list(self.exp_dir.glob("*.yaml"))
        if not yaml_files:
            raise FileNotFoundError(f"No .yaml files found in {self.exp_dir}")

        workload_path = yaml_files[0]
        with open(workload_path) as f:
            workload_spec = yaml.safe_load(f)

        # Extract baseline rate from first cohort
        if "cohorts" not in workload_spec or not workload_spec["cohorts"]:
            raise ValueError("Workload spec has no cohorts")

        first_cohort = workload_spec["cohorts"][0]
        baseline_rate = first_cohort["spike"]["trace_rate"]

        # Validate all cohorts have same trace rate (uniform scaling required)
        for i, cohort in enumerate(workload_spec["cohorts"]):
            cohort_rate = cohort["spike"]["trace_rate"]
            if abs(cohort_rate - baseline_rate) > 1e-6:
                raise ValueError(
                    f"All cohorts must have same trace_rate for uniform scaling. "
                    f"Cohort {i} has {cohort_rate}, expected {baseline_rate}"
                )

        return workload_spec, baseline_rate


class WorkloadSpecGenerator:
    """Generate modified workload YAML with new trace rate."""

    def __init__(self, base_workload: dict):
        self.base_workload = base_workload

    def generate(self, target_rate: float) -> Path:
        """Generate temporary YAML file with modified trace rate.

        Args:
            target_rate: New trace rate for all cohorts

        Returns:
            Path to temporary YAML file
        """
        # Deep copy to avoid modifying original
        modified = copy.deepcopy(self.base_workload)

        # Modify all cohort trace rates
        for cohort in modified["cohorts"]:
            cohort["spike"]["trace_rate"] = target_rate

        # Write to temp file
        fd, temp_path = tempfile.mkstemp(suffix=".yaml", prefix=f"workload_{target_rate}_")
        os.close(fd)  # Close the file descriptor, we'll write with yaml.dump

        with open(temp_path, "w") as f:
            yaml.dump(modified, f)

        return Path(temp_path)


class BLISRunner:
    """Execute BLIS and parse saturation verdict."""

    def __init__(self, blis_binary: str, server_config: dict):
        self.blis_binary = blis_binary
        self.server_config = server_config

    def _build_command(self, workload_path: Path, output_path: Path) -> list[str]:
        """Build BLIS command from configuration.

        Maps experiment.json fields to BLIS CLI flags:
        - model → --model
        - hw → --hardware
        - tp → --tp
        - chunk_size → --long-prefill-token-threshold
        - gpu_mem → --gpu-memory-utilization
        - scheduling → --scheduler (with name mapping)
        - mbt → --max-num-scheduled-tokens
        - max_model_len → --max-model-len
        - kv_offload → --kv-cpu-blocks (false → 0 for single-tier)

        Ignored metadata fields (not BLIS flags):
        - workload, arrival_pattern, notes, harness, precision, dp

        Args:
            workload_path: Path to workload YAML
            output_path: Path for BLIS output JSON

        Returns:
            Command as list of strings
        """
        # Normalize model name to lowercase for model config path
        model_name = self.server_config["model"]
        model_config_name = model_name.lower()

        # Map scheduler names: experiment.json uses "priority" but BLIS expects "priority-fcfs"
        scheduler_map = {
            "priority": "priority-fcfs",
            "fcfs": "fcfs",
            "sjf": "sjf",
            "reverse-priority": "reverse-priority"
        }
        scheduler = scheduler_map.get(self.server_config["scheduling"], self.server_config["scheduling"])

        # Derive paths relative to BLIS binary
        blis_dir = Path(self.blis_binary).parent
        model_config_folder = blis_dir / "model_configs" / model_config_name
        hardware_config = blis_dir / "hardware_config.json"
        defaults_filepath = blis_dir / "defaults.yaml"

        cmd = [
            self.blis_binary,
            "run",
            "--workload-spec", str(workload_path),
            "--latency-model", "trained-physics",
            "--post-hoc-detector", "composite",
            "--metrics-path", str(output_path),
            "--model", model_name,
            "--model-config-folder", str(model_config_folder),
            "--hardware-config", str(hardware_config),
            "--defaults-filepath", str(defaults_filepath),
            "--tp", str(self.server_config["tp"]),
            "--hardware", self.server_config["hw"],
            "--gpu-memory-utilization", str(self.server_config["gpu_mem"]),
            "--scheduler", scheduler,
            "--max-num-scheduled-tokens", str(self.server_config["mbt"]),
            "--max-model-len", str(self.server_config["max_model_len"])
        ]

        # Add optional chunked prefill threshold if specified
        if "chunk_size" in self.server_config and self.server_config["chunk_size"]:
            cmd.extend(["--long-prefill-token-threshold", str(self.server_config["chunk_size"])])

        # Handle KV offload configuration
        # If kv_offload is false, explicitly disable by setting kv-cpu-blocks to 0 (single-tier mode)
        if "kv_offload" in self.server_config and self.server_config["kv_offload"] is False:
            cmd.extend(["--kv-cpu-blocks", "0"])

        # Add horizon for 25-minute simulation (arrivals happen in first 10 minutes)
        # 1500 seconds = 1,500,000,000 microseconds = 1,500,000,000 ticks
        cmd.extend(["--horizon", "1500000000"])

        # Add per-request timeout of 11 minutes (660 seconds)
        cmd.extend(["--timeout", "660"])

        return cmd

    def _parse_verdict(self, output_path: Path) -> tuple[str, dict]:
        """Parse saturation verdict from BLIS output JSON.

        Args:
            output_path: Path to BLIS output JSON

        Returns:
            (verdict string, saturation signals dict)
        """
        if not output_path.exists():
            raise FileNotFoundError(f"BLIS output not found: {output_path}")

        with open(output_path) as f:
            output = json.load(f)

        if "saturation" not in output:
            raise ValueError("BLIS output missing 'saturation' field")

        saturation = output["saturation"]
        verdict = saturation["level"]

        return verdict, saturation

    def run(self, workload_path: Path, output_path: Path, timeout: int = 1800) -> tuple[str, dict, dict]:
        """Execute BLIS and return saturation verdict.

        Args:
            workload_path: Path to workload YAML
            output_path: Path for BLIS output JSON
            timeout: Timeout in seconds (default 1800 = 30 minutes)

        Returns:
            (verdict string, saturation signals dict, metadata dict)

        Raises:
            RuntimeError: If BLIS execution fails
            TimeoutExpired: If execution exceeds timeout
        """
        cmd = self._build_command(workload_path, output_path)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True
            )

            verdict, signals = self._parse_verdict(output_path)

            metadata = {
                "exit_code": 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "command": " ".join(cmd)
            }

            return verdict, signals, metadata

        except subprocess.TimeoutExpired as e:
            raise TimeoutError(f"BLIS execution exceeded {timeout}s timeout") from e

        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"BLIS execution failed (exit code {e.returncode})\n"
                f"stderr: {e.stderr}"
            ) from e


class SaturationSearcher:
    """Orchestrate two-phase search for saturation point."""

    def __init__(self, baseline_rate: float, min_multiplier: float = 0.01, max_multiplier: float = 100.0, coarse_precision: float = 50.0):
        self.baseline_rate = baseline_rate
        self.min_multiplier = min_multiplier
        self.max_multiplier = max_multiplier
        self.coarse_precision = coarse_precision
        self.all_results = []  # Store all test results

    def _binary_search(self, run_blis_fn, min_rate: float, max_rate: float, precision: float, phase_name: str) -> tuple[float, float]:
        """Execute binary search between rate bounds.

        Args:
            run_blis_fn: Function that takes rate and returns (verdict, signals, metadata)
            min_rate: Lower bound (known or assumed STABLE)
            max_rate: Upper bound (known or assumed OVERLOADED)
            precision: Stop when bounds within this many RPS
            phase_name: Name for logging ("coarse" or "fine")

        Returns:
            (stable_rate, overloaded_rate) tuple
        """
        stable_rate = min_rate
        overloaded_rate = max_rate

        iteration = 0
        while (overloaded_rate - stable_rate) > precision:
            iteration += 1
            mid_rate = (stable_rate + overloaded_rate) / 2.0
            mid_mult = mid_rate / self.baseline_rate
            current_range = overloaded_rate - stable_rate

            print(f"  [{iteration}] Testing {mid_rate:.2f} RPS ({mid_mult:.3f}x, range: {current_range:.2f} RPS)...", end=" ", flush=True)

            verdict, signals, metadata = run_blis_fn(mid_rate)
            print(f"{verdict}")

            self.all_results.append({
                "rate_rps": mid_rate,
                "multiplier": mid_mult,
                "phase": phase_name,
                "verdict": verdict,
                "saturation": signals
            })

            # Treat BACKLOGGED as STABLE (not overloaded yet)
            if verdict in ["STABLE", "BACKLOGGED"]:
                stable_rate = mid_rate
            else:  # OVERLOADED
                overloaded_rate = mid_rate

        return stable_rate, overloaded_rate

    def search(self, run_blis_fn, precision: float = 1.0) -> float:
        """Execute complete two-phase saturation point search.

        Args:
            run_blis_fn: Function that takes rate and returns (verdict, signals, metadata)
            precision: Binary search precision in RPS (for Phase 2)

        Returns:
            Saturation point (highest STABLE rate)

        Raises:
            RuntimeError: If unable to find saturation point
        """
        print(f"\nPhase 1: Exponential search from baseline (1.0x)")

        # Test baseline first to determine direction
        baseline_rate = self.baseline_rate
        print(f"  [1] Testing {baseline_rate:.2f} RPS (1.0x)...", end=" ", flush=True)

        verdict, signals, metadata = run_blis_fn(baseline_rate)
        print(f"{verdict}")

        self.all_results.append({
            "rate_rps": baseline_rate,
            "multiplier": 1.0,
            "phase": "coarse",
            "verdict": verdict,
            "saturation": signals
        })

        baseline_stable = verdict in ["STABLE", "BACKLOGGED"]

        # Exponential search to find bracket
        stable_rate = None
        overloaded_rate = None

        if baseline_stable:
            # Baseline is stable → exponentially search upward: 2x, 4x, 8x, ...
            print(f"  Baseline STABLE → exponential search upward (2x, 4x, 8x, ...)")
            stable_rate = baseline_rate
            multiplier = 2.0
            iteration = 2

            while multiplier <= self.max_multiplier:
                rate = self.baseline_rate * multiplier
                print(f"  [{iteration}] Testing {rate:.2f} RPS ({multiplier:.1f}x)...", end=" ", flush=True)

                verdict, signals, metadata = run_blis_fn(rate)
                print(f"{verdict}")

                self.all_results.append({
                    "rate_rps": rate,
                    "multiplier": multiplier,
                    "phase": "coarse",
                    "verdict": verdict,
                    "saturation": signals
                })

                if verdict in ["STABLE", "BACKLOGGED"]:
                    stable_rate = rate
                else:  # OVERLOADED
                    overloaded_rate = rate
                    print(f"  ✓ Found bracket: [{stable_rate:.2f} RPS STABLE, {overloaded_rate:.2f} RPS OVERLOADED]")
                    break

                multiplier *= 2
                iteration += 1

            # Check if we never found overload
            if overloaded_rate is None:
                print(f"  System remains STABLE up to {stable_rate:.2f} RPS ({stable_rate/self.baseline_rate:.1f}x)")
                return stable_rate

        else:
            # Baseline is overloaded → exponentially search downward: 0.5x, 0.25x, 0.125x, ...
            print(f"  Baseline OVERLOADED → exponential search downward (0.5x, 0.25x, 0.125x, ...)")
            overloaded_rate = baseline_rate
            multiplier = 0.5
            iteration = 2

            while multiplier >= self.min_multiplier:
                rate = self.baseline_rate * multiplier
                print(f"  [{iteration}] Testing {rate:.2f} RPS ({multiplier:.3f}x)...", end=" ", flush=True)

                verdict, signals, metadata = run_blis_fn(rate)
                print(f"{verdict}")

                self.all_results.append({
                    "rate_rps": rate,
                    "multiplier": multiplier,
                    "phase": "coarse",
                    "verdict": verdict,
                    "saturation": signals
                })

                if verdict in ["STABLE", "BACKLOGGED"]:
                    stable_rate = rate
                    print(f"  ✓ Found bracket: [{stable_rate:.2f} RPS STABLE, {overloaded_rate:.2f} RPS OVERLOADED]")
                    break
                else:  # OVERLOADED
                    overloaded_rate = rate

                multiplier *= 0.5
                iteration += 1

            # Check if we never found stable
            if stable_rate is None:
                raise RuntimeError(
                    f"System is saturated even at minimal load ({overloaded_rate:.2f} RPS = {overloaded_rate/self.baseline_rate:.3f}x)."
                )

        # Phase 1b: Binary search within exponential bracket to refine to coarse precision
        print(f"\nPhase 1b: Binary search within bracket (precision: {self.coarse_precision:.1f} RPS)")
        stable_coarse, overloaded_coarse = self._binary_search(
            run_blis_fn, stable_rate, overloaded_rate, self.coarse_precision, "coarse"
        )

        print(f"  Refined bracket: [{stable_coarse:.2f} RPS STABLE, {overloaded_coarse:.2f} RPS OVERLOADED]")

        # Phase 2: Fine binary search within the bracket
        print(f"\nPhase 2: Fine binary search (precision: {precision:.1f} RPS)")

        stable_fine, overloaded_fine = self._binary_search(
            run_blis_fn, stable_coarse, overloaded_coarse, precision, "fine"
        )

        final_precision = overloaded_fine - stable_fine
        print(f"  ✓ Converged: {stable_fine:.2f} RPS (precision: {final_precision:.2f} RPS)")

        return stable_fine


class ResultReporter:
    """Format and save saturation search results."""

    def __init__(self, exp_id: str, baseline_rate: float, saturation_point: float, all_results: list):
        self.exp_id = exp_id
        self.baseline_rate = baseline_rate
        self.saturation_point = saturation_point
        self.all_results = all_results

    def print_console(self):
        """Print formatted console output."""
        print(f"\nSaturation Point Detector for {self.exp_id}")
        print("=" * 60)
        print(f"Baseline rate: {self.baseline_rate:.2f} RPS")
        print()

        # Phase 1: Coarse search
        coarse_results = [r for r in self.all_results if r["phase"] == "coarse"]
        if coarse_results:
            print("Phase 1: Coarse-grained search")
            for r in coarse_results:
                print(f"  Testing {r['rate_rps']:.2f} RPS ({r['multiplier']:.2f}x): {r['verdict']}")

            stable_rates = [r['rate_rps'] for r in coarse_results if r['verdict'] in ['STABLE', 'BACKLOGGED']]
            overloaded_rates = [r['rate_rps'] for r in coarse_results if r['verdict'] == 'OVERLOADED']

            if stable_rates and overloaded_rates:
                print(f"  Bracketed: [{max(stable_rates):.2f} RPS STABLE, {min(overloaded_rates):.2f} RPS OVERLOADED]")
            print()

        # Phase 2: Fine search
        fine_results = [r for r in self.all_results if r["phase"] == "fine"]
        if fine_results:
            print("Phase 2: Fine-grained binary search")
            for r in fine_results:
                print(f"  Testing {r['rate_rps']:.2f} RPS: {r['verdict']}")

            # Calculate final precision
            stable_rates = [r['rate_rps'] for r in fine_results if r['verdict'] in ['STABLE', 'BACKLOGGED']]
            overloaded_rates = [r['rate_rps'] for r in fine_results if r['verdict'] == 'OVERLOADED']

            if stable_rates and overloaded_rates:
                final_precision = min(overloaded_rates) - max(stable_rates)
                print(f"  Converged (precision: {final_precision:.2f} RPS)")
            print()

        # Final result
        print("=" * 60)
        print(f"SATURATION POINT: {self.saturation_point:.2f} RPS")
        print("  (Highest STABLE rate, system becomes OVERLOADED above this)")
        print()

    def save_json(self, output_path: Path, config: dict, search_params: dict):
        """Save complete results to JSON file.

        Args:
            output_path: Where to save JSON
            config: Configuration dict (model, hardware, etc)
            search_params: Search parameters (precision, etc)
        """
        coarse_results = [r for r in self.all_results if r["phase"] == "coarse"]
        fine_results = [r for r in self.all_results if r["phase"] == "fine"]

        # Calculate final precision
        stable_rates = [r['rate_rps'] for r in self.all_results if r['verdict'] in ['STABLE', 'BACKLOGGED']]
        overloaded_rates = [r['rate_rps'] for r in self.all_results if r['verdict'] == 'OVERLOADED']

        final_precision = 0.0
        if stable_rates and overloaded_rates:
            final_precision = min(overloaded_rates) - max(stable_rates)

        output = {
            "experiment_id": self.exp_id,
            "configuration": config,
            "search_parameters": search_params,
            "all_runs": self.all_results,
            "result": {
                "saturation_point_rps": self.saturation_point,
                "saturation_multiplier": self.saturation_point / self.baseline_rate,
                "interpretation": "Highest STABLE rate. System becomes OVERLOADED above this threshold.",
                "total_runs": len(self.all_results),
                "coarse_runs": len(coarse_results),
                "fine_runs": len(fine_results),
                "final_precision_rps": final_precision
            },
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z")
        }

        with open(output_path, "w") as f:
            json.dump(output, f, indent=2)


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Find saturation point for LLM serving workload using BLIS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s exp1
  %(prog)s exp2 --precision 10
  %(prog)s exp1 --verbose
        """
    )

    parser.add_argument(
        "exp_id",
        help="Experiment ID (e.g., 'exp1') - looks for <base_dir>/<exp_id>/"
    )

    parser.add_argument(
        "--base-dir",
        default=".",
        help="Base directory for experiments (default: current directory)"
    )

    parser.add_argument(
        "--blis-binary",
        default="../inference-sim/blis",
        help="Path to BLIS binary (default: ../inference-sim/blis)"
    )

    parser.add_argument(
        "--precision",
        type=float,
        default=1.0,
        help="Phase 2 binary search precision in RPS (default: 1.0)"
    )

    parser.add_argument(
        "--min-multiplier",
        type=float,
        default=0.01,
        help="Minimum rate multiplier for Phase 1 (default: 0.01)"
    )

    parser.add_argument(
        "--max-multiplier",
        type=float,
        default=100.0,
        help="Maximum rate multiplier for Phase 1 (default: 100.0)"
    )

    parser.add_argument(
        "--coarse-precision",
        type=float,
        default=50.0,
        help="Phase 1 binary search precision in RPS (default: 50.0)"
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable detailed logging"
    )

    return parser.parse_args()


def main():
    """Main entry point for saturation point detection."""
    args = parse_args()

    if args.verbose:
        print(f"Loading experiment: {args.exp_id}")

    try:
        # 1. Load experiment configuration
        loader = ExperimentLoader(args.exp_id, base_dir=args.base_dir)
        server_config = loader.load_server_config()
        workload_spec, baseline_rate = loader.load_workload_spec()

        if args.verbose:
            print(f"Server config: {server_config['model']} on {server_config['hw']}")
            print(f"Baseline rate: {baseline_rate:.2f} RPS")

        # 2. Initialize components
        spec_generator = WorkloadSpecGenerator(workload_spec)
        blis_runner = BLISRunner(args.blis_binary, server_config)
        searcher = SaturationSearcher(
            baseline_rate=baseline_rate,
            min_multiplier=args.min_multiplier,
            max_multiplier=args.max_multiplier,
            coarse_precision=args.coarse_precision
        )

        # 3. Define BLIS execution wrapper
        def run_blis(rate: float):
            """Run BLIS for a specific trace rate."""
            # Generate modified workload
            workload_path = spec_generator.generate(rate)

            # Create temp output path
            fd, output_path_str = tempfile.mkstemp(suffix=".json", prefix=f"blis_{rate}_")
            os.close(fd)
            output_path = Path(output_path_str)

            try:
                verdict, signals, metadata = blis_runner.run(workload_path, output_path)
                return verdict, signals, metadata

            finally:
                # Cleanup temp files
                if workload_path.exists():
                    workload_path.unlink()
                if output_path.exists():
                    output_path.unlink()

        # 4. Execute search
        print(f"\n{'='*60}")
        print(f"Saturation Point Search for {args.exp_id}")
        print(f"{'='*60}")
        print(f"Model: {server_config['model']} on {server_config['hw']}, TP={server_config['tp']}")
        print(f"Baseline rate: {baseline_rate:.2f} RPS")
        print(f"Target precision: {args.precision} RPS")

        saturation_point = searcher.search(run_blis, precision=args.precision)

        # 5. Report results
        reporter = ResultReporter(
            exp_id=args.exp_id,
            baseline_rate=baseline_rate,
            saturation_point=saturation_point,
            all_results=searcher.all_results
        )

        reporter.print_console()

        # Save JSON
        output_path = loader.exp_dir / "saturation_results.json"
        reporter.save_json(
            output_path,
            config={
                "model": server_config["model"],
                "hardware": server_config["hw"],
                "tp": server_config["tp"],
                "baseline_rate": baseline_rate,
                "blis_latency_model": "trained-physics",
                "saturation_detector": "composite"
            },
            search_params={
                "phase1_min_multiplier": args.min_multiplier,
                "phase1_max_multiplier": args.max_multiplier,
                "phase1_coarse_precision_rps": args.coarse_precision,
                "phase2_fine_precision_rps": args.precision
            }
        )

        print(f"Results saved to: {output_path}")

        return 0

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
