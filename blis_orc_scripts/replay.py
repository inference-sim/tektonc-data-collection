#!/usr/bin/env python3
"""Local BLIS replay script.

Runs replay phase on downloaded observe data for one or more experiments.
Automatically clones/builds BLIS if needed.
"""
import argparse
import json
import subprocess
import sys
import yaml
from pathlib import Path


def load_models_config():
    """Load models.yaml from blis-campaign/config."""
    models_yaml = Path(__file__).parent.parent / "blis-campaign" / "config" / "models.yaml"
    if not models_yaml.exists():
        return {}
    with open(models_yaml) as f:
        return yaml.safe_load(f)


def resolve_model_name(short_name, models_config):
    """Resolve short model name to full HuggingFace ID.

    Args:
        short_name: Short name like "Llama-3.1-8b"
        models_config: Dict from models.yaml

    Returns:
        Full HF model ID like "meta-llama/Llama-3.1-8B-Instruct"
    """
    if short_name not in models_config:
        # If not in config, assume it's already a full HF ID
        return short_name

    entry = models_config[short_name]
    if isinstance(entry, str):
        # Simple string mapping
        return entry
    elif isinstance(entry, dict) and "hf_id" in entry:
        # Dict with hf_id key
        return entry["hf_id"]
    else:
        # Fallback to original name
        return short_name


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


def get_downloaded_data_dir(exp_dir):
    """Get the downloaded data directory path for an experiment."""
    # Check if there's a downloaded data directory
    # Campaign structure: campaign/XX-model-hw-workload/data/XX-...-tp-dlp/
    # Legacy: observe-data-XX/ or XX-model-hw-workload-X-X/

    exp_json = exp_dir / "experiment.json"
    if not exp_json.exists():
        raise ValueError(f"experiment.json not found in {exp_dir}")

    with open(exp_json) as f:
        exp = json.load(f)

    exp_id = exp["id"]
    tp = exp.get("tp", 1)
    dp = exp.get("dp", 1)

    # Build experiment ID with tp-dp suffix (matches PVC directory name)
    model_slug = exp["model"].replace("/", "-")
    exp_id_full = f"{exp_id}-{model_slug}-tp{tp}-{exp['workload']}-{tp}-{dp}"

    # Check canonical campaign data location first
    campaign_data = exp_dir / "data"
    if campaign_data.exists():
        # Find subdirectory matching experiment pattern
        for subdir in campaign_data.iterdir():
            if subdir.is_dir() and (subdir / "observe" / "header.yaml").exists():
                return subdir / "observe"

    # Try legacy patterns
    possible_paths = [
        Path.cwd() / f"observe-data-{exp_id}",
        Path.cwd() / f"observe-data-{exp_id}" / "trace",
        exp_dir.parent.parent / f"observe-data-{exp_id}",
        exp_dir.parent.parent / f"observe-data-{exp_id}" / "trace",
    ]

    # Also check for downloaded dirs with full experiment name
    exp_name = exp_dir.name
    possible_paths.extend([
        Path.cwd() / exp_name,
        exp_dir.parent.parent / exp_name,
    ])

    for path in possible_paths:
        if path.exists() and (path / "observe" / "header.yaml").exists():
            return path / "observe"
        if path.exists() and (path / "header.yaml").exists():
            return path

    raise ValueError(
        f"Downloaded observe data not found for experiment {exp_id}. "
        f"Expected to find observe/header.yaml and observe/data.csv. "
        f"Tried campaign data dir: {campaign_data}, legacy paths: {[str(p) for p in possible_paths]}"
    )


def run_replay(blis_binary, exp_dir, data_dir, model_config_folder, models_config):
    """Run BLIS replay on downloaded observe data."""
    exp_json = exp_dir / "experiment.json"
    with open(exp_json) as f:
        exp = json.load(f)

    # Read experiment parameters from experiment.json
    # Resolve short model name to full HuggingFace ID
    short_model_name = exp["model"]
    model = resolve_model_name(short_model_name, models_config)
    if model != short_model_name:
        print(f"   Model: {short_model_name} → {model}")
    tp = exp.get("tp", 1)
    hw = exp["hw"]
    latency_model = exp.get("latency_model", "trained-physics")
    gpu_mem = exp.get("gpu_mem", 0.9)

    # Read vLLM configuration from exp-config.yaml (if available)
    exp_config_path = data_dir.parent / "exp-config.yaml"
    max_num_seqs = 256  # BLIS default
    max_num_batched_tokens = 2048  # BLIS default
    max_model_len = 0  # BLIS default (unlimited, auto-derived from model config)

    if exp_config_path.exists():
        with open(exp_config_path) as f:
            exp_config = yaml.safe_load(f)

        # Map vLLM parameter names to values
        max_num_seqs = exp_config.get("max_num_seqs", max_num_seqs)
        max_num_batched_tokens = exp_config.get("max_num_batched_tokens", max_num_batched_tokens)
        max_model_len = exp_config.get("max_model_len", max_model_len)

    # Create replay output directory
    replay_dir = data_dir.parent / "replay"
    replay_dir.mkdir(parents=True, exist_ok=True)

    # Build command (use absolute paths since BLIS runs from repo dir)
    cmd = [
        str(blis_binary), "replay",
        "--trace-header", str((data_dir / "header.yaml").resolve()),
        "--trace-data", str((data_dir / "data.csv").resolve()),
        "--latency-model", latency_model,
        "--model", model,
        "--tp", str(tp),
        "--hardware", hw,
        "--max-num-running-reqs", str(max_num_seqs),
        "--max-num-scheduled-tokens", str(max_num_batched_tokens),
        "--gpu-memory-utilization", str(gpu_mem),
        "--results-path", str((replay_dir / "sim_result.json").resolve()),
    ]

    # Add max-model-len if specified (non-zero)
    if max_model_len > 0:
        cmd.extend(["--max-model-len", str(max_model_len)])

    # Add model config folder if provided
    if model_config_folder:
        cmd.extend(["--model-config-folder", str(Path(model_config_folder).resolve())])

    print(f"\n🔄 Running replay for experiment {exp['id']} ({exp['model']} on {exp['hw']})...")
    print(f"   Observe data: {data_dir}")
    print(f"   Replay output: {replay_dir}")
    print(f"   vLLM config: max_num_seqs={max_num_seqs}, max_num_batched_tokens={max_num_batched_tokens}, gpu_mem={gpu_mem}")
    if max_model_len > 0:
        print(f"                max_model_len={max_model_len}")
    print(f"   Command: {' '.join(cmd)}")

    # Run replay (change to BLIS repo dir so it finds bundled configs)
    result = subprocess.run(
        cmd,
        cwd=blis_binary.parent,
        capture_output=True,
        text=True
    )

    # Save logs
    (replay_dir / "stdout.log").write_text(result.stdout)
    (replay_dir / "stderr.log").write_text(result.stderr)

    if result.returncode != 0:
        print(f"❌ Replay failed for experiment {exp['id']}")
        print(f"   stderr: {result.stderr[:500]}")
        return False

    print(f"✅ Replay completed for experiment {exp['id']}")
    print(f"   Output: {replay_dir / 'sim_result.json'}")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Run BLIS replay locally on downloaded observe data"
    )
    parser.add_argument(
        "--experiment-ids",
        required=True,
        help="Comma-separated experiment IDs (e.g., '68' or '68,69,70')"
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
        default=None,
        help="Path to model config folder (optional, for HF model configs)"
    )

    args = parser.parse_args()

    # Parse experiment IDs
    exp_ids = [int(x.strip()) for x in args.experiment_ids.split(",")]

    # Ensure BLIS is built
    blis_repo_path = Path(args.blis_repo).resolve()
    blis_binary = ensure_blis_built(blis_repo_path)

    # Load model name mappings
    models_config = load_models_config()

    # Find experiment directories
    exp_dirs = find_experiment_dirs(args.campaign, exp_ids)
    if not exp_dirs:
        print("❌ No experiment directories found")
        return 1

    # Run replay for each experiment
    successes = 0
    failures = 0

    for exp_dir in exp_dirs:
        try:
            data_dir = get_downloaded_data_dir(exp_dir)
            success = run_replay(blis_binary, exp_dir, data_dir, args.model_config_folder, models_config)
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
