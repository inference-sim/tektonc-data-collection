#!/usr/bin/env python3
"""Local BLIS replay script.

Runs replay phase on downloaded observe data for one or more experiments.
Automatically clones/builds BLIS if needed.
"""
import argparse
import json
import re
import shlex
import subprocess
import sys
import yaml
from pathlib import Path


def kv_blocks_from_serve_log(data_dir, block_size):
    """Derive --total-kv-blocks from the real `vllm serve` log, if present.

    The observe phase deploys the model with `vllm serve`, and that pod's log
    (vllm.log, downloaded alongside the trace) records the exact KV cache the
    served engine allocated:

        GPU KV cache size: 15,703,524 tokens

    That token count already includes the full serving-path memory profiling
    (weights + real activation peak + cudagraph), so dividing by BLIS's block
    size gives the authoritative --total-kv-blocks. This is strictly better than
    letting BLIS auto-calculate: BLIS's weight estimator breaks on quantized /
    hybrid checkpoints (e.g. it computed 190 GiB of weights for a 20 GiB NVFP4
    model and aborted). Using the served number sidesteps that entirely.

    Looks for vllm.log next to the data dir (data_dir/.. and data_dir/../..).
    Returns (total_kv_blocks, kv_tokens, log_path) or (None, None, None) if no
    usable log line is found (caller then falls back to BLIS auto-calc).
    """
    token_re = re.compile(r"GPU KV cache size:\s*([\d,]+)\s*tokens")
    for log_path in _serve_log_candidates(data_dir):
        try:
            text = log_path.read_text(errors="replace")
        except Exception:
            continue
        matches = token_re.findall(text)
        if matches:
            kv_tokens = int(matches[-1].replace(",", ""))  # last = live engine
            return kv_tokens // block_size, kv_tokens, log_path
    return None, None, None


def _serve_log_candidates(data_dir):
    """Yield existing vllm.log paths near the observe data dir."""
    for p in (data_dir.parent / "vllm.log", data_dir.parent.parent / "vllm.log"):
        if p.exists():
            yield p


def cpu_blocks_from_serve_log(data_dir):
    """Read the CPU offload block count the real `vllm serve` engine allocated.

    `SimpleCPUOffloadConnector` logs its CPU tier size at INFO, e.g.:

        SimpleCPUOffloadWorker: 6 unique GPU KV tensors, allocating 1642 CPU blocks (10.00 GB)

    Those CPU blocks are in the model's NATIVE block units (e.g. 2128 tokens for
    the hybrid Nemotron, not 16), so this is the authoritative count to hand BLIS
    as --kv-cpu-blocks -- strictly better than the 4 MiB/block heuristic, which
    assumes dense-model 16-token blocks and mis-sizes hybrids. Returns
    (cpu_blocks, log_path) or (None, None) if the connector didn't log it (e.g.
    the v0.26 OffloadingConnector, which logs CPU blocks only at DEBUG).
    """
    # "allocating N CPU blocks" (SimpleCPUOffloadWorker/Scheduler INFO line).
    cpu_re = re.compile(r"allocating\s+([\d,]+)\s+CPU blocks")
    for log_path in _serve_log_candidates(data_dir):
        try:
            text = log_path.read_text(errors="replace")
        except Exception:
            continue
        matches = cpu_re.findall(text)
        if matches:
            return int(matches[-1].replace(",", "")), log_path
    return None, None


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
        # Try both hyphen and underscore patterns
        exp_id_str = str(exp_id)
        matches = list(campaign_path.glob(f"{exp_id_str}-*"))
        if not matches:
            # Try underscore pattern (e.g., exp1_overloaded)
            matches = list(campaign_path.glob(f"{exp_id_str}_*"))
        if not matches:
            # Try exact match (e.g., directory name = experiment ID)
            exact_match = campaign_path / exp_id_str
            if exact_match.exists() and exact_match.is_dir():
                matches = [exact_match]
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
    dp = exp.get("dp") or 1
    hw = exp["hw"]
    latency_model = exp.get("latency_model", "trained-physics")
    gpu_mem = exp.get("gpu_mem", 0.9)

    # Read vLLM configuration from exp-config.yaml (if available)
    exp_config_path = data_dir.parent / "exp-config.yaml"
    max_num_seqs = 256  # BLIS default
    max_num_batched_tokens = 2048  # BLIS default
    max_model_len = 0  # BLIS default (unlimited, auto-derived from model config)
    block_size = 16  # BLIS default
    kv_offloading_gb = 0  # CPU offloading disabled by default

    if exp_config_path.exists():
        with open(exp_config_path) as f:
            exp_config = yaml.safe_load(f)

        # Map vLLM parameter names to values
        max_num_seqs = exp_config.get("max_num_seqs", max_num_seqs)
        max_num_batched_tokens = exp_config.get("max_num_batched_tokens", max_num_batched_tokens)
        max_model_len = exp_config.get("max_model_len", max_model_len)
        block_size = exp_config.get("block_size", block_size)
        kv_offloading_gb = exp_config.get("kv_offloading_size", 0)

    # Check for large prefixes and warn if KV thrashing is likely
    # NOTE: We trust exp-config.yaml values (which come from the real server)
    # rather than overriding them with heuristics. The real server successfully
    # ran with these settings, so the simulator should match them for accurate calibration.
    import csv
    max_prefix_length = 0
    with open(data_dir / "data.csv") as f:
        reader = csv.DictReader(f)
        for row in reader:
            prefix_len = int(row.get("prefix_length", 0))
            if prefix_len > max_prefix_length:
                max_prefix_length = prefix_len

    if max_prefix_length > 8192:  # Large prefixes (>8K tokens)
        # Estimate KV blocks needed per request using configured block_size
        blocks_per_prefix = max_prefix_length // block_size
        # Estimate total KV blocks (80GB GPU * gpu_mem / block_size)
        # Rough estimate: 3909 blocks for H100 80GB at 0.9 utilization
        estimated_total_blocks = 3909 if hw == "H100" else 2500  # Conservative for other GPUs
        # Safe concurrency = total_blocks / blocks_per_prefix, with safety margin
        safe_max_num_seqs = max(4, int(estimated_total_blocks / blocks_per_prefix * 0.8))

        if max_num_seqs > safe_max_num_seqs:
            # WARN but don't override - exp-config.yaml captures the real server config
            print(f"   ⚠️  Large prefixes detected (max={max_prefix_length} tokens)")
            print(f"   ⚠️  WARNING: max_num_seqs={max_num_seqs} may exceed KV capacity (estimated safe: {safe_max_num_seqs})")
            print(f"   ⚠️  However, using exp-config.yaml value to match real server configuration")
            print(f"   ℹ️  NOTE: Real vLLM may use chunked prefill + CPU offload, allowing higher concurrency")

    # Create replay output directory
    replay_dir = data_dir.parent / "replay"
    replay_dir.mkdir(parents=True, exist_ok=True)

    # Get BLIS repo directory for config files
    blis_repo_dir = blis_binary.parent
    defaults_path = blis_repo_dir / "defaults.yaml"
    hardware_config_path = blis_repo_dir / "hardware_config.json"

    # Build command (use absolute paths)
    cmd = [
        str(blis_binary), "replay",
        "--trace-header", str((data_dir / "header.yaml").resolve()),
        "--trace-data", str((data_dir / "data.csv").resolve()),
        "--defaults-filepath", str(defaults_path.resolve()),
        "--hardware-config", str(hardware_config_path.resolve()),
        "--latency-model", latency_model,
        "--model", model,
        "--tp", str(tp),
        "--hardware", hw,
        "--max-num-running-reqs", str(max_num_seqs),
        "--max-num-scheduled-tokens", str(max_num_batched_tokens),
        "--block-size-in-tokens", str(block_size),
        "--gpu-memory-utilization", str(gpu_mem),
        "--results-path", str((replay_dir / "sim_result.json").resolve()),
        "--log", "info",
    ]

    # GPU KV tier: use the size the REAL vllm serve engine allocated (from the
    # downloaded serve log) instead of BLIS's own auto-calculation. BLIS's weight
    # estimator mis-sizes quantized / hybrid checkpoints and can abort KV
    # auto-calc entirely; the served number is exact and always safe. Passing
    # --total-kv-blocks makes BLIS skip its auto-calc (see cmd/root.go).
    #
    # NOTE on GPU vs CPU tiers: vLLM logs the GPU KV size at INFO ("GPU KV cache
    # size: N tokens"), which we read here for --total-kv-blocks (the GPU tier).
    # The CPU offload tier (--kv-cpu-blocks, below) is a SEPARATE number and is
    # NOT reliably in the serve log (vLLM logs it only at DEBUG); it is derived
    # from the configured cpu_bytes_to_use instead. The two are distinct flags to
    # BLIS and are kept independent -- the serve-log value is GPU-tier only.
    gpu_kv_blocks, kv_tokens, kv_log = kv_blocks_from_serve_log(data_dir, block_size)
    if gpu_kv_blocks:
        cmd.extend(["--total-kv-blocks", str(gpu_kv_blocks)])
        print(f"   GPU KV blocks: {gpu_kv_blocks} (from vllm serve log: "
              f"{kv_tokens:,} tokens ÷ block_size {block_size})")
    else:
        print("   GPU KV blocks: no vllm.log found; letting BLIS auto-calculate "
              "(may fail for quantized/hybrid models)")

    # Add routing scorer for dense models with dp > 1
    if dp > 1 and is_dense_model(short_model_name, models_config):
        cmd.extend(["--num-instances", str(dp)])
        cmd.extend(["--routing-scorers", "vllm-dp:1"])

    # Add max-model-len if specified (non-zero)
    if max_model_len > 0:
        cmd.extend(["--max-model-len", str(max_model_len)])

    # CPU KV offload tier — use BLIS's modern --kv-offload-config path (#1583/#1587),
    # NOT the legacy --kv-cpu-blocks flag. Rationale:
    #   * --kv-cpu-blocks reuses the single GPU BlockSizeTokens (16), but vLLM's CPU
    #     tier is in the model's native block units (2128 tokens for hybrid Nemotron),
    #     so a raw block count is ~133x wrong AND the new BLIS refuses to combine it
    #     with a header/flag offload config.
    #   * --kv-offload-config takes cpu_bytes_to_use + vLLM block_size; BLIS derives
    #     per_block_bytes from the model's KV size internally (correct units). For an
    #     observed (mode "real") trace with no kv_offload header block, BLIS's #1583
    #     allowFlagAdd path accepts this flag to model the observed deployment.
    # Prefer experiment.json's kv_connector_config (the source of truth) over the
    # derived exp-config.yaml GiB. cpu_bytes_to_use is taken verbatim; block_size
    # falls back to the GPU block size. BLIS's --kv-offload-config wants a file path
    # (no inline CLI form), so we materialize a tiny YAML from those values -- the
    # file is just BLIS's required interface, not a separate source of config.
    kvc = exp.get("kv_connector_config") or {}
    cpu_bytes = kvc.get("cpu_bytes_to_use")
    if cpu_bytes is None and kv_offloading_gb > 0:
        cpu_bytes = int(round(kv_offloading_gb * 1024**3))  # fallback: pre-nested-config dirs
    if cpu_bytes:
        offload_block_size = kvc.get("block_size", block_size)
        kv_offload_yaml = (
            "kv_offload:\n"
            f"  cpu_bytes_to_use: {int(cpu_bytes)}\n"
            f"  block_size: {int(offload_block_size)}\n"
        )
        kv_offload_path = replay_dir / "kv_offload.yaml"
        kv_offload_path.write_text(kv_offload_yaml)
        cmd.extend(["--kv-offload-config", str(kv_offload_path.resolve())])
        print(f"   CPU offload: --kv-offload-config (cpu_bytes_to_use={int(cpu_bytes)}, "
              f"block_size={int(offload_block_size)}; from "
              f"{'experiment.json kv_connector_config' if kvc.get('cpu_bytes_to_use') else 'exp-config.yaml fallback'}); "
              f"BLIS derives per-block bytes from the model")

    # Add model config folder if provided
    if model_config_folder:
        cmd.extend(["--model-config-folder", str(Path(model_config_folder).resolve())])

    print(f"\n🔄 Running replay for experiment {exp['id']} ({exp['model']} on {exp['hw']})...")
    print(f"   Observe data: {data_dir}")
    print(f"   Replay output: {replay_dir}")
    print(f"   Latency model: {latency_model}")
    if dp > 1 and is_dense_model(short_model_name, models_config):
        print(f"   Routing: vllm-dp (data-parallel, {dp} instances)")
    print(f"   vLLM config: max_num_seqs={max_num_seqs}, max_num_batched_tokens={max_num_batched_tokens}, block_size={block_size}, gpu_mem={gpu_mem}")
    if max_model_len > 0:
        print(f"                max_model_len={max_model_len}")
    print(f"   Command: {' '.join(cmd)}")

    # Persist the exact replay invocation for reproducibility. Written BEFORE the
    # run so it survives even if replay hangs or crashes. `command_line` is a
    # copy-pasteable string (run from `cwd`); `argv` is the unjoined list.
    command_record = {
        "experiment_id": exp["id"],
        "model": model,
        "cwd": str(blis_binary.parent),
        "argv": cmd,
        "command_line": shlex.join(cmd),
        "status": "running",
    }
    command_record_path = replay_dir / "replay_command.json"
    command_record_path.write_text(json.dumps(command_record, indent=2))

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

    # Update the command record with the outcome now that the run has finished.
    command_record["status"] = "succeeded" if result.returncode == 0 else "failed"
    command_record["returncode"] = result.returncode
    command_record_path.write_text(json.dumps(command_record, indent=2))

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

    # Parse experiment IDs (accept both numeric and string IDs)
    exp_ids = []
    for x in args.experiment_ids.split(","):
        x = x.strip()
        # Try to convert to int, but keep as string if it fails
        try:
            exp_ids.append(int(x))
        except ValueError:
            exp_ids.append(x)

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
