#!/usr/bin/env python3
"""Local BLIS run script.

Runs forward simulation (blis run) for experiments defined in experiments.json.
Uses the same server configuration as would be used in the real deployment.
"""
import argparse
import json
import subprocess
import sys
import yaml
from pathlib import Path


def ensure_blis_built(blis_repo_path):
    """Clone and build BLIS if not already present."""
    if not blis_repo_path.exists():
        print(f"📦 Cloning inference-sim to {blis_repo_path}...")
        subprocess.run(
            ["git", "clone", "https://github.com/inference-sim/inference-sim.git", str(blis_repo_path)],
            check=True
        )

    blis_binary = blis_repo_path / "blis"
    if not blis_binary.exists():
        print("🔨 Building BLIS binary...")
        subprocess.run(
            ["go", "build", "-o", "blis", "main.go"],
            cwd=blis_repo_path,
            check=True
        )

    if not blis_binary.exists():
        raise RuntimeError(f"BLIS binary not found at {blis_binary} after build")

    print(f"✅ BLIS ready at {blis_binary}")
    return blis_binary


def load_experiments(path):
    """Load experiments.json."""
    with open(path) as f:
        return json.load(f)


def load_workloads(path):
    """Load workloads.yaml."""
    with open(path) as f:
        return yaml.safe_load(f)


def load_models_config():
    """Load models.yaml config."""
    models_path = Path(__file__).parent.parent / "blis-campaign" / "config" / "models.yaml"
    with open(models_path) as f:
        return yaml.safe_load(f)


def resolve_model_name(short_name, models_config):
    """Resolve short model name to full HuggingFace ID."""
    if short_name not in models_config:
        return short_name
    entry = models_config[short_name]
    if isinstance(entry, str):
        return entry
    return entry.get("hf_id", short_name)


def is_dense_model(short_name, models_config):
    """Check if model is a dense architecture (not MoE).

    Args:
        short_name: Short model name like "Llama-3.1-8b"
        models_config: Dict from models.yaml

    Returns:
        bool: True if dense, False if MoE or architecture unknown
    """
    if short_name not in models_config:
        # Unknown model, default to False (don't add routing scorer)
        return False

    entry = models_config[short_name]
    if isinstance(entry, dict):
        # Check architecture field, default to "dense" for backward compatibility
        return entry.get("architecture", "dense") == "dense"

    # Simple string entry (legacy format), assume dense
    return True


def find_experiment_by_id(experiments, exp_id):
    """Find experiment by ID."""
    for exp in experiments:
        if exp["id"] == exp_id:
            return exp
    raise ValueError(f"Experiment {exp_id} not found in experiments.json")


def find_experiment_dirs(campaign_dir, experiment_ids):
    """Find experiment directories matching the given IDs."""
    campaign_path = Path(campaign_dir)
    if not campaign_path.exists():
        raise ValueError(f"Campaign directory not found: {campaign_dir}")

    exp_dirs = []
    for exp_id in experiment_ids:
        # Find directories starting with the experiment ID
        matches = list(campaign_path.glob(f"{exp_id}-*"))
        if not matches:
            print(f"⚠️  No campaign directory found for experiment {exp_id}")
            continue
        if len(matches) > 1:
            print(f"⚠️  Multiple directories found for experiment {exp_id}: {matches}")
            print(f"   Using first match: {matches[0]}")
        exp_dirs.append(matches[0])

    return exp_dirs


def get_or_create_data_dir(exp_dir, exp):
    """Get or create data directory for experiment (matches replay.py pattern)."""
    exp_id = exp["id"]
    tp = exp.get("tp", 1)
    dlp = exp.get("dp", 1) if exp.get("dp") else 1

    # Build experiment ID with tp-dlp suffix (matches PVC directory name)
    model_slug = exp["model"].replace("/", "-").lower()
    workload_slug = exp["workload"].replace("_", "-").lower()

    # Pattern: {exp_id}-{model}-tp{tp}-{workload}-{tp}-{dlp}
    data_subdir_name = f"{exp_id}-{model_slug}-tp{tp}-{workload_slug}-{tp}-{dlp}"

    # Check if data directory exists
    data_base = exp_dir / "data"
    if data_base.exists():
        # Try to find matching subdirectory
        for subdir in data_base.iterdir():
            if subdir.is_dir() and subdir.name.startswith(f"{exp_id}-"):
                return subdir

    # Create new data directory
    data_dir = data_base / data_subdir_name
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def run_experiment(blis_binary, exp, workloads, models_config, exp_dir, model_config_folder):
    """Run BLIS forward simulation for an experiment."""
    exp_id = exp["id"]

    # Resolve model name
    short_model_name = exp["model"]
    model = resolve_model_name(short_model_name, models_config)

    # Get workload
    workload_name = exp["workload"]
    if workload_name not in workloads:
        raise ValueError(f"Workload '{workload_name}' not found in workloads.yaml")

    workload = workloads[workload_name]
    spec_type = workload.get("spec", "inference_perf")

    # Get server configuration
    tp = exp["tp"]
    dp = exp.get("dp", 1) if exp.get("dp") else 1
    hw = exp["hw"]
    gpu_mem = exp.get("gpu_mem", 0.9)
    max_num_seqs = exp.get("max_num_seqs", 256)
    max_num_batched_tokens = exp.get("mbt", 2048)
    max_model_len = exp.get("max_model_len", 0)
    block_size = exp.get("block_size", 16)
    kv_offloading_gb = 8.0 if exp.get("cpu_offload", False) else 0

    # Get or create data directory (matches replay.py structure)
    data_dir = get_or_create_data_dir(exp_dir, exp)

    # Create run output directory
    run_dir = data_dir / "run"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Get BLIS repo directory for config files
    blis_repo_dir = blis_binary.parent
    defaults_path = blis_repo_dir / "defaults.yaml"
    hardware_config_path = blis_repo_dir / "hardware_config.json"

    # Build workload YAML file
    workload_file = run_dir / "workload.yaml"

    if spec_type == "blis_native":
        # Use BLIS native format directly
        with open(workload_file, "w") as f:
            yaml.dump(workload["blis"], f, default_flow_style=False, sort_keys=False)
    elif spec_type == "inference_perf":
        # Convert inference_perf format to BLIS native
        # Calculate total requests and aggregate rate from stages
        stages = workload["load"]["stages"]
        horizon = sum(stage["duration"] for stage in stages)
        total_requests = sum(int(stage["rate"] * stage["duration"]) for stage in stages)
        aggregate_rate = total_requests / horizon if horizon > 0 else 0

        blis_workload = {
            "version": "2",
            "seed": 42,
            "category": "language",
            "aggregate_rate": aggregate_rate,
            "num_requests": total_requests,
            "clients": [
                {
                    "id": "default",
                    "slo_class": "standard",
                    "rate_fraction": 1.0,
                    "streaming": True,
                    "arrival": {"process": "poisson"},
                    "input_distribution": {
                        "type": "gaussian",
                        "params": {
                            "mean": workload["data"]["shared_prefix"]["question_len"] +
                                   workload["data"]["shared_prefix"]["system_prompt_len"],
                            "std_dev": 128,
                            "min": 64,
                            "max": 2048
                        }
                    },
                    "output_distribution": {
                        "type": "gaussian",
                        "params": {
                            "mean": workload["data"]["shared_prefix"]["output_len"],
                            "std_dev": 64,
                            "min": 32,
                            "max": 512
                        }
                    }
                }
            ]
        }

        # Add prefix if using shared_prefix
        if workload["data"]["type"] == "shared_prefix":
            blis_workload["clients"][0]["prefix_group"] = "system-prompt"
            blis_workload["clients"][0]["prefix_length"] = workload["data"]["shared_prefix"]["system_prompt_len"]

        with open(workload_file, "w") as f:
            yaml.dump(blis_workload, f, default_flow_style=False, sort_keys=False)
    else:
        raise ValueError(f"Unsupported workload spec type: {spec_type}")

    # Build command
    cmd = [
        str(blis_binary), "run",
        "--workload", str(workload_file.resolve()),
        "--defaults-filepath", str(defaults_path.resolve()),
        "--hardware-config", str(hardware_config_path.resolve()),
        "--model", model,
        "--tp", str(tp),
        "--hardware", hw,
        "--max-num-running-reqs", str(max_num_seqs),
        "--max-num-scheduled-tokens", str(max_num_batched_tokens),
        "--block-size-in-tokens", str(block_size),
        "--gpu-memory-utilization", str(gpu_mem),
        "--results-path", str((run_dir / "sim_result.json").resolve()),
        "--log", "info",
    ]

    # Add routing scorer for dense models with dp > 1
    if dp > 1 and is_dense_model(short_model_name, models_config):
        cmd.extend(["--num-instances", str(dp)])
        cmd.extend(["--routing-scorers", "vllm-dp:1"])

    # Add max-model-len if specified (non-zero)
    if max_model_len > 0:
        cmd.extend(["--max-model-len", str(max_model_len)])

    # Add CPU offloading if configured
    if kv_offloading_gb > 0:
        # Heuristic: 4 MiB per block → 256 blocks per GiB
        cpu_blocks = int(kv_offloading_gb * 256)
        cmd.extend([
            "--kv-cpu-blocks", str(cpu_blocks),
            "--kv-offload-threshold", "0.9",
            "--kv-transfer-bandwidth", "0.2"
        ])
        print(f"   CPU offload: {kv_offloading_gb} GB → {cpu_blocks} blocks (4 MiB/block)")

    # Add model config folder if provided
    if model_config_folder:
        cmd.extend(["--model-config-folder", str(Path(model_config_folder).resolve())])

    print(f"\n🚀 Running forward simulation for experiment {exp_id} ({exp['model']} on {exp['hw']})...")
    print(f"   Workload: {workload_name}")
    print(f"   Data directory: {data_dir}")
    print(f"   Output: {run_dir}")
    print(f"   Server config: max_num_seqs={max_num_seqs}, max_num_batched_tokens={max_num_batched_tokens}, block_size={block_size}, gpu_mem={gpu_mem}")
    if max_model_len > 0:
        print(f"                  max_model_len={max_model_len}")
    if dp > 1 and is_dense_model(short_model_name, models_config):
        print(f"   Routing: vllm-dp (data-parallel, {dp} instances)")
    print(f"   Command: {' '.join(cmd)}")

    # Run simulation
    result = subprocess.run(
        cmd,
        cwd=blis_binary.parent,
        capture_output=True,
        text=True
    )

    # Save logs
    (run_dir / "stdout.log").write_text(result.stdout)
    (run_dir / "stderr.log").write_text(result.stderr)

    if result.returncode != 0:
        print(f"❌ Simulation failed for experiment {exp_id}")
        print(f"   stderr: {result.stderr[:500]}")
        return False

    print(f"✅ Simulation completed for experiment {exp_id}")
    print(f"   Output: {run_dir / 'sim_result.json'}")

    # Print summary if result exists
    result_path = run_dir / "sim_result.json"
    if result_path.exists():
        try:
            with open(result_path) as f:
                results = json.load(f)
            print(f"   📊 Simulation summary:")
            if "summary" in results:
                summary = results["summary"]
                if "throughput_rps" in summary:
                    print(f"      Throughput: {summary['throughput_rps']:.2f} req/s")
                if "p50_latency_ms" in summary:
                    print(f"      P50 latency: {summary['p50_latency_ms']:.2f} ms")
                if "p99_latency_ms" in summary:
                    print(f"      P99 latency: {summary['p99_latency_ms']:.2f} ms")
        except Exception as e:
            print(f"   ⚠️  Could not parse simulation results: {e}")

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Run BLIS forward simulation locally for experiments"
    )
    parser.add_argument(
        "--experiment-ids",
        required=True,
        help="Comma-separated experiment IDs (e.g., '68' or '68,69,70')"
    )
    parser.add_argument(
        "--experiments",
        default="blis-campaign/experiments.json",
        help="Path to experiments.json (default: blis-campaign/experiments.json)"
    )
    parser.add_argument(
        "--workloads",
        default="workloads.yaml",
        help="Path to workloads.yaml (default: workloads.yaml)"
    )
    parser.add_argument(
        "--campaign",
        default="blis-campaign/campaign",
        help="Campaign directory (default: blis-campaign/campaign)"
    )
    parser.add_argument(
        "--blis-repo",
        default="../inference-sim",
        help="Path to inference-sim repo (will clone if not present)"
    )
    parser.add_argument(
        "--model-config-folder",
        help="Path to model config folder (optional)"
    )

    args = parser.parse_args()

    # Parse experiment IDs
    exp_ids = [int(x.strip()) for x in args.experiment_ids.split(",")]

    # Ensure BLIS is built
    blis_repo_path = Path(args.blis_repo).resolve()
    blis_binary = ensure_blis_built(blis_repo_path)

    # Load configurations
    experiments = load_experiments(args.experiments)
    workloads = load_workloads(args.workloads)
    models_config = load_models_config()

    # Find experiment directories in campaign
    exp_dirs = find_experiment_dirs(args.campaign, exp_ids)
    if not exp_dirs:
        print("❌ No experiment directories found")
        return 1

    # Run simulations for each experiment
    successes = 0
    failures = 0

    for exp_dir in exp_dirs:
        try:
            # Load experiment.json from directory
            exp_json = exp_dir / "experiment.json"
            if not exp_json.exists():
                print(f"⚠️  No experiment.json found in {exp_dir}")
                continue

            with open(exp_json) as f:
                exp = json.load(f)

            success = run_experiment(
                blis_binary, exp, workloads, models_config,
                exp_dir, args.model_config_folder
            )
            if success:
                successes += 1
            else:
                failures += 1
        except Exception as e:
            print(f"❌ Error processing {exp_dir.name}: {e}")
            failures += 1

    print(f"\n📊 Summary: {successes} succeeded, {failures} failed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
