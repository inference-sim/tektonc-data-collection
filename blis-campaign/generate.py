"""Experiment YAML generator for BLIS campaign.

Reads experiments.json + config files, builds per-experiment values.yaml
overrides, calls tektonc for pipeline compilation, and generates pipelinerun.yaml.
"""
import copy
import json
import re
import subprocess
import sys
import yaml
from pathlib import Path


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_yaml(path):
    """Load YAML file."""
    with open(path) as f:
        return yaml.safe_load(f)


def write_yaml(path, data):
    """Write data to YAML file."""
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, width=200)


def write_json(path, data):
    """Write data to JSON file."""
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_experiments(path):
    """Load and return experiments list from JSON."""
    with open(path) as f:
        return json.load(f)


def load_models(path):
    """Load models.yaml config."""
    return load_yaml(path)


def load_clusters(path):
    """Load clusters.yaml config."""
    return load_yaml(path)


def load_workloads(path):
    """Load workloads.yaml config."""
    return load_yaml(path)


# ---------------------------------------------------------------------------
# Validation (collect all errors, don't fail on first)
# ---------------------------------------------------------------------------

def validate_all(experiments, models, clusters, workloads):
    """Validate all experiments. Returns list of error strings (empty = valid)."""
    errors = []
    valid_hw = {k for k in clusters if k != "namespace"}
    valid_harnesses = {"inference-perf", "orc"}

    for exp in experiments:
        eid = exp.get("id", "?")
        if exp["model"] not in models:
            errors.append(f"Experiment #{eid}: unknown model '{exp['model']}'")
        if exp["hw"] not in valid_hw:
            errors.append(f"Experiment #{eid}: unknown hw '{exp['hw']}'")
        if exp["workload"] not in workloads:
            errors.append(f"Experiment #{eid}: unknown workload '{exp['workload']}'")

        # Validate harness field (optional, defaults to inference-perf)
        harness = exp.get("harness", "inference-perf")
        if harness not in valid_harnesses:
            errors.append(f"Experiment #{eid}: unknown harness '{harness}' (valid: {valid_harnesses})")

        # Validate workload spec compatibility with harness
        if exp["workload"] in workloads:
            wl = workloads[exp["workload"]]
            spec_type = wl.get("spec", "inference_perf")

            if spec_type == "blis_native" and harness != "orc":
                errors.append(
                    f"Experiment #{eid}: workload '{exp['workload']}' has spec=blis_native "
                    f"but harness={harness} (must be 'orc')"
                )

    return errors


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def make_dns_name(s):
    """Convert string to DNS-1123 compatible name."""
    s = s.lower()
    s = re.sub(r'[^a-z0-9-]', '-', s)
    s = re.sub(r'-+', '-', s)
    s = s.strip('-')
    return s[:63]


def make_dir_name(exp):
    """e.g. '13-qwen3-14b-h100-general'"""
    return make_dns_name(f"{exp['id']}-{exp['model']}-{exp['hw']}-{exp['workload']}")


def make_experiment_id(exp):
    """e.g. '13-qwen3-14b-tp1-general' -- used as PVC data path and Helm label."""
    return make_dns_name(f"{exp['id']}-{exp['model']}-tp{exp['tp']}-{exp['workload']}")


def resolve_model(name, models, precision=None):
    """Returns (hf_id, extra_args_list, is_prequantized).

    When precision='FP8' and the model has an fp8_hf_id, returns the
    pre-quantized checkpoint ID and sets is_prequantized=True so the
    caller knows to skip the --quantization=fp8 vLLM arg.
    """
    entry = models[name]
    if isinstance(entry, str):
        return entry, [], False
    hf_id = entry["hf_id"]
    extra = entry.get("extra_vllm_args", [])
    # Use pre-quantized FP8 checkpoint if available
    if precision == "FP8" and "fp8_hf_id" in entry:
        return entry["fp8_hf_id"], extra, True
    return hf_id, extra, False


def extract_pipeline_name(pipeline_yaml_path):
    """Read compiled pipeline.yaml and extract metadata.name."""
    data = load_yaml(pipeline_yaml_path)
    return data["metadata"]["name"]


# ---------------------------------------------------------------------------
# Values builder (the core logic)
# ---------------------------------------------------------------------------

DEFAULT_KV_OFFLOAD_GB = 8.0
MOE_MODELS = {"Mixtral-8x7B", "DeepSeek-V3", "Llama-4-Scout-17B-16E"}


def build_values(exp, base_values, models, clusters, workloads):
    """Build per-experiment values.yaml from base template + experiment config."""
    v = copy.deepcopy(base_values)

    # Resolve model (use pre-quantized FP8 checkpoint when available)
    hf_id, extra_args, is_prequantized = resolve_model(
        exp["model"], models, precision=exp["precision"]
    )

    # Experiment identity
    exp_name = make_dns_name(f"blis-{exp['id']}-{exp['model']}-{exp['workload']}")
    v["experiment"]["name"] = exp_name
    v["experiment"]["description"] = (
        f"Exp #{exp['id']}: {exp['model']} {exp['precision']} "
        f"TP{exp['tp']} {exp['workload']} on {exp['hw']}"
    )

    # Stack config
    v["stack"]["MAX_NUM_BATCHED_TOKENS"] = exp["mbt"]
    # Allow experiments.json to override MAX_MODEL_LEN (default from base values if not specified)
    if "max_model_len" in exp:
        v["stack"]["MAX_MODEL_LEN"] = exp["max_model_len"]
    # Allow experiments.json to override MAX_NUM_SEQS (default from base values if not specified)
    if "max_num_seqs" in exp:
        v["stack"]["MAX_NUM_SEQS"] = exp["max_num_seqs"]
    # Allow experiments.json to override BLOCK_SIZE (default from base values if not specified)
    if "block_size" in exp:
        v["stack"]["BLOCK_SIZE"] = exp["block_size"]
    v["stack"]["treatments"]["tensorParallelism"] = [exp["tp"]]
    v["stack"]["treatments"]["dataLocalParallelism"] = [exp.get("dp") or 1]

    # Store cpu_offload flag and offloading size for template access
    v["stack"]["cpu_offload"] = exp.get("cpu_offload", False)
    v["stack"]["kv_offloading_size"] = DEFAULT_KV_OFFLOAD_GB if exp.get("cpu_offload") else 0

    # GPU targeting
    cluster = clusters[exp["hw"]]
    v["stack"]["model"]["helmValues"]["decode"]["acceleratorTypes"]["labelValues"] = [
        cluster["gpu_label_value"]
    ]

    # GPU reaper exclusion — prevent reaper from killing experiment deployments
    decode = v["stack"]["model"]["helmValues"]["decode"]
    if "annotations" not in decode:
        decode["annotations"] = {}
    decode["annotations"]["gpu-reaper.io/exclude"] = "true"

    # Build extra_overrides (handles ALL capacity-related vLLM args including CPU offloading)
    # Observability template only handles observability features, NOT capacity management
    v["stack"]["extra_overrides"] = build_extra_overrides(
        exp, extra_args, is_prequantized=is_prequantized
    )

    # Workload profile - translate based on harness type and spec
    wl = workloads[exp["workload"]]
    harness = exp.get("harness", "inference-perf")  # Default to inference-perf for backward compatibility
    spec_type = wl.get("spec", "inference_perf")    # Default to inference_perf for backward compatibility

    if harness == "orc":
        # ORC harness: use appropriate format
        if spec_type == "inference_perf":
            # Pass through inference-perf format directly to BLIS observe
            # Reference: inference-sim/testdata/trained_physics_iter29.json
            # Calculate total requests and horizon from stages
            horizon = sum(stage["duration"] for stage in wl["load"]["stages"])
            total_requests = sum(int(stage["rate"] * stage["duration"]) for stage in wl["load"]["stages"])

            v["workload"]["orcSpec"] = {
                "version": "2",
                "seed": 42,
                "num_requests": total_requests,
                "inference_perf": {
                    "stages": wl["load"]["stages"],
                    "shared_prefix": wl["data"]["shared_prefix"]
                }
            }
            v["workload"]["horizon"] = horizon
        elif spec_type == "blis_native":
            # Use BLIS native format directly (cohorts, diurnal, multi-client, etc.)
            v["workload"]["orcSpec"] = wl["blis"]
            # Calculate horizon from num_requests and aggregate_rate
            # Horizon is a time bound (seconds) for pipeline scheduling
            # Use 2× mean to account for bursty arrival processes (gamma, diurnal, etc.)
            num_requests = wl["blis"].get("num_requests", 0)
            aggregate_rate = wl["blis"].get("aggregate_rate", 1.0)
            horizon = int(2 * num_requests / aggregate_rate) if aggregate_rate > 0 else 0
            v["workload"]["horizon"] = horizon
        else:
            raise ValueError(f"Unsupported workload spec type: {spec_type}")
    else:
        # inference-perf harness (default)
        if spec_type == "inference_perf":
            # Use existing format
            v["workload"]["profileTemplate"]["load"] = wl["load"]
            v["workload"]["profileTemplate"]["data"] = wl["data"]
        elif spec_type == "blis_native":
            # Error: can't run BLIS-native workload on inference-perf harness
            raise ValueError(
                f"Workload '{exp['workload']}' has spec=blis_native "
                f"and cannot be used with harness=inference-perf"
            )
        else:
            raise ValueError(f"Unsupported workload spec type: {spec_type}")

    return v


def build_extra_overrides(exp, model_extra_args, is_prequantized=False):
    """Build the list of Helm override strings for extra vLLM args.

    This handles ALL capacity-related vLLM configuration including CPU offloading.
    Observability templates handle ONLY observability features (tracing, KV events).
    """
    overrides = []

    # FP8 quantization — skip if using a pre-quantized checkpoint
    # (weights are already FP8 on disk, vLLM auto-detects from config.json)
    if exp["precision"] == "FP8" and not is_prequantized:
        overrides.append(
            'decode.containers[name="vllm"].args=--quantization=fp8'
        )

    # GPU memory utilization (only if non-default)
    if exp["gpu_mem"] != 0.9:
        overrides.append(
            f'decode.containers[name="vllm"].args=--gpu-memory-utilization={exp["gpu_mem"]}'
        )

    # KV cache offloading (total GiB across TP ranks) - CAPACITY MANAGEMENT
    # Stock vLLM v0.15.1 enables HMA by default which conflicts with
    # OffloadingConnector, so we also disable HMA when offloading is on.
    # NOTE: This is independent of observability - observability can run with or without offloading
    if exp.get("cpu_offload"):
        overrides.append(
            f'decode.containers[name="vllm"].args=--kv-offloading-size={DEFAULT_KV_OFFLOAD_GB}'
        )
        overrides.append(
            'decode.containers[name="vllm"].args=--disable-hybrid-kv-cache-manager'
        )

    # Expert parallelism for MoE models with data-local parallelism > 1
    dp = exp.get("dp")
    if dp and dp > 1 and exp["model"] in MOE_MODELS:
        overrides.append(
            'decode.containers[name="vllm"].args=--enable-expert-parallel'
        )

    # Model-specific extra args
    for arg in model_extra_args:
        overrides.append(f'decode.containers[name="vllm"].args={arg}')

    return overrides


# ---------------------------------------------------------------------------
# PipelineRun builder
# ---------------------------------------------------------------------------

PIPELINERUN_TEMPLATE = """\
apiVersion: tekton.dev/v1
kind: PipelineRun
metadata:
  name: __PIPELINE_RUN_NAME__
spec:
  timeouts:
    pipeline: 6h
    tasks: 5h30m
  taskRunTemplate:
    serviceAccountName: helm-installer
  workspaces:
    - name: model-cache
      persistentVolumeClaim:
        claimName: model-pvc
    - name: data
      persistentVolumeClaim:
        claimName: data-pvc
    - name: hf-credentials
      secret:
        secretName: hf-secret
        items:
          - key: HF_TOKEN
            path: HF_TOKEN
    - name: target-credentials
      secret:
        secretName: s3-secret
        items:
          - key: AWS_ACCESS_KEY_ID
            path: AWS_ACCESS_KEY_ID
          - key: AWS_SECRET_ACCESS_KEY
            path: AWS_SECRET_ACCESS_KEY
  pipelineRef:
    name: {pipeline_name}
  params:
    - {{ name: experimentId, value: "{experiment_id}" }}
    - {{ name: model, value: "{hf_model}" }}
    - {{ name: namespace, value: "{namespace}" }}
"""


def build_pipelinerun(pipeline_name, experiment_id, hf_model, namespace):
    """Build pipelinerun.yaml content."""
    return PIPELINERUN_TEMPLATE.format(
        pipeline_name=pipeline_name,
        experiment_id=experiment_id,
        hf_model=hf_model,
        namespace=namespace,
    )


# ---------------------------------------------------------------------------
# Main generate loop
# ---------------------------------------------------------------------------

def generate_campaign(args):
    """Main entry point for 'blis-campaign generate'."""
    config_dir = Path(__file__).parent / "config"
    experiments = load_experiments(args.experiments)
    models = load_models(config_dir / "models.yaml")
    clusters = load_clusters(config_dir / "clusters.yaml")
    workloads = load_workloads(
        Path(__file__).parent.parent / "workloads.yaml"
    )

    # Load base values for each harness type
    tektoncsample_base = Path(__file__).parent.parent / "tektoncsample"

    # inference-perf harness base values (stock and observability)
    base_values_inference_perf_stock = load_yaml(
        tektoncsample_base / "blis-inference-perf" / "values.yaml"
    )
    base_values_inference_perf_observability = load_yaml(
        tektoncsample_base / "blis-inference-perf" / "values-observability.yaml"
    )

    # ORC harness base values (stock and observability)
    base_values_orc_stock = load_yaml(
        tektoncsample_base / "blis-orc" / "values.yaml"
    )
    base_values_orc_observability = load_yaml(
        tektoncsample_base / "blis-orc" / "values-observability.yaml"
    )

    # Filter to specific IDs if --only is given
    only_ids = None
    if getattr(args, "only", None):
        only_ids = {int(x.strip()) for x in args.only.split(",")}
        experiments = [e for e in experiments if e["id"] in only_ids]

    # Skip done experiments unless --all or --only
    include_all = getattr(args, "all", False)
    if not include_all and not only_ids:
        experiments = [e for e in experiments if not e.get("done", False)]

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Validate all experiments first (fail-fast)
    errors = validate_all(experiments, models, clusters, workloads)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1

    # Template paths for different harnesses
    tektoncsample_base = Path(__file__).parent.parent / "tektoncsample"
    TEMPLATE_PATHS = {
        "inference-perf": tektoncsample_base / "blis-inference-perf" / "data_pipeline.yaml.j2",
        "orc": tektoncsample_base / "blis-orc" / "data_pipeline.yaml.j2",
    }

    generated = 0
    for exp in experiments:
        # Select template based on harness (default to inference-perf)
        harness = exp.get("harness", "inference-perf")
        if harness not in TEMPLATE_PATHS:
            print(f"ERROR: Unknown harness type '{harness}' for experiment #{exp['id']}", file=sys.stderr)
            return 1
        template_path = TEMPLATE_PATHS[harness]
        dir_name = make_dir_name(exp)
        exp_dir = output_dir / dir_name
        exp_dir.mkdir(parents=True, exist_ok=True)

        # 1. Save experiment.json (copy of this experiment's entry)
        write_json(exp_dir / "experiment.json", exp)

        # 2. Select base values based on harness and observability flag
        harness = exp.get("harness", "inference-perf")
        use_observability = exp.get("observability", False)

        if harness == "orc":
            # ORC harness (supports observability)
            base_values = (base_values_orc_observability if use_observability
                          else base_values_orc_stock)
        else:
            # inference-perf harness (default)
            base_values = (base_values_inference_perf_observability if use_observability
                          else base_values_inference_perf_stock)

        # 3. Build and save values.yaml
        values = build_values(exp, base_values, models, clusters, workloads)
        write_yaml(exp_dir / "values.yaml", values)

        # 4. Compile pipeline.yaml via tektonc
        result = subprocess.run(
            [sys.executable,
             str(Path(__file__).parent.parent / "tektonc/tektonc.py"),
             "-t", str(template_path),
             "-f", str(exp_dir / "values.yaml"),
             "-o", str(exp_dir / "pipeline.yaml")],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"FATAL: tektonc failed for experiment #{exp['id']}:\n{result.stderr}",
                  file=sys.stderr)
            return 1

        # 5. Extract pipeline name and generate pipelinerun.yaml
        pipeline_name = extract_pipeline_name(exp_dir / "pipeline.yaml")
        hf_id, _, _ = resolve_model(exp["model"], models, precision=exp["precision"])
        experiment_id = make_experiment_id(exp)
        pr_yaml = build_pipelinerun(
            pipeline_name, experiment_id, hf_id, clusters["namespace"]
        )
        (exp_dir / "pipelinerun.yaml").write_text(pr_yaml)

        generated += 1
        obs_marker = " [obs]" if use_observability else ""
        harness_marker = f" [{harness}]" if harness != "inference-perf" else ""
        print(f"  [{generated}/{len(experiments)}] #{exp['id']} {exp['model']} "
              f"{exp['hw']} {exp['workload']}{obs_marker}{harness_marker}")

    print(f"Generated {generated} experiments in {output_dir}/")
    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate BLIS campaign experiments")
    parser.add_argument("--experiments", required=True, help="Path to experiments.json")
    parser.add_argument("--output", required=True, help="Output directory for campaign")
    parser.add_argument("--only", help="Comma-separated list of experiment IDs to generate")
    args = parser.parse_args()
    sys.exit(generate_campaign(args))
