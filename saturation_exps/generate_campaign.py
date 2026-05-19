# saturation_exps/generate_campaign.py
"""Generate BLIS campaign pipelines from saturation experiment folders.

Reads saturation_results.json, updates workload trace_rate to saturation point,
generates values.yaml, and compiles Tekton pipeline YAML.
"""
import argparse
from pathlib import Path


def parse_args(argv=None):
    """Parse command-line arguments.

    Args:
        argv: Optional argument list (for testing)

    Returns:
        argparse.Namespace with experiments list
    """
    parser = argparse.ArgumentParser(
        description="Generate BLIS campaign from saturation experiments"
    )
    parser.add_argument(
        "--experiments",
        required=True,
        help="Comma-separated list of experiment folders (e.g., exp1,exp3,exp5)"
    )
    args = parser.parse_args(argv)
    # Split comma-separated list into array
    args.experiments = [e.strip() for e in args.experiments.split(",")]
    return args


def find_workload_file(exp_dir):
    """Find the workload YAML file in experiment directory.

    Args:
        exp_dir: Path to experiment directory

    Returns:
        Path to workload YAML file

    Raises:
        FileNotFoundError: If no workload YAML found
        ValueError: If multiple workload YAMLs found
    """
    # Exclude generated files
    exclude_files = {"values.yaml", "pipeline.yaml", "pipelinerun.yaml"}

    # Find all YAML files except excluded ones
    yaml_files = [
        f for f in exp_dir.glob("*.yaml")
        if f.name not in exclude_files
    ]

    if len(yaml_files) == 0:
        raise FileNotFoundError(
            f"No workload YAML file found in {exp_dir} "
            "(expected saturation_*.yaml or similar)"
        )

    if len(yaml_files) > 1:
        file_list = ", ".join(f.name for f in yaml_files)
        raise ValueError(
            f"Multiple workload files found in {exp_dir}: {file_list}. "
            "Expected exactly one YAML file."
        )

    return yaml_files[0]



import json
import yaml
import subprocess
from datetime import datetime


def update_workload_trace_rate(workload_data, saturation_rps):
    """Update all cohort trace_rate values to saturation point RPS.

    Args:
        workload_data: Parsed workload YAML dict
        saturation_rps: Saturation point RPS value

    Returns:
        Updated workload dict (modifies in-place and returns)

    Raises:
        ValueError: If workload structure is invalid
    """
    if "cohorts" not in workload_data:
        raise ValueError("Invalid workload: no cohorts array")

    for cohort in workload_data["cohorts"]:
        cohort_id = cohort.get("id", "unknown")

        if "spike" not in cohort:
            raise ValueError(f"Cohort {cohort_id} missing spike section")

        if "trace_rate" not in cohort["spike"]:
            raise ValueError(f"Cohort {cohort_id} missing spike.trace_rate field")

        # Update trace_rate to saturation point
        cohort["spike"]["trace_rate"] = saturation_rps

    return workload_data


def extract_variant_rates(saturation_results: dict) -> tuple[float, float]:
    """
    Extract saturation and overloaded rates from saturation results.

    Args:
        saturation_results: Parsed saturation_results.json dict

    Returns:
        (saturation_rate, overloaded_rate) where:
        - saturation_rate = result.saturation_point_rps
        - overloaded_rate = result.saturation_point_rps + result.final_precision_rps
    """
    result = saturation_results["result"]
    saturation_rate = result["saturation_point_rps"]
    overloaded_rate = saturation_rate + result["final_precision_rps"]
    return saturation_rate, overloaded_rate


def create_variant_workload(workload_data, variant_rate, output_path):
    """Create workload file with specific trace_rate for a variant.

    Args:
        workload_data: Original workload dict
        variant_rate: RPS value for this variant
        output_path: Path where variant workload should be written

    Returns:
        Updated workload dict
    """
    import copy

    # Deep copy to avoid modifying original
    variant_workload = copy.deepcopy(workload_data)

    # Update all cohort trace_rate values
    updated = update_workload_trace_rate(variant_workload, variant_rate)

    # Write to output path
    write_yaml(output_path, updated)

    return updated


def load_json(path):
    """Load JSON file.

    Args:
        path: Path to JSON file

    Returns:
        Parsed JSON data

    Raises:
        FileNotFoundError: If file doesn't exist
        json.JSONDecodeError: If JSON is invalid
    """
    with open(path) as f:
        return json.load(f)


def load_yaml(path):
    """Load YAML file.

    Args:
        path: Path to YAML file

    Returns:
        Parsed YAML data

    Raises:
        FileNotFoundError: If file doesn't exist
        yaml.YAMLError: If YAML is invalid
    """
    with open(path) as f:
        return yaml.safe_load(f)


def write_yaml(path, data):
    """Write data to YAML file.

    Args:
        path: Path to output YAML file
        data: Data to serialize
    """
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, width=200)


import copy
import re


DEFAULT_KV_OFFLOAD_GB = 8.0
MOE_MODELS = {"Mixtral-8x7B", "DeepSeek-V3", "Llama-4-Scout-17B-16E"}


def make_dns_name(s):
    """Convert string to DNS-1123 compatible name."""
    s = s.lower()
    s = re.sub(r'[^a-z0-9-]', '-', s)
    s = re.sub(r'-+', '-', s)
    s = s.strip('-')
    return s[:63]


# resolve_model() removed - experiment.json now contains full HuggingFace IDs directly


def build_extra_overrides(experiment):
    """Build the list of Helm override strings for extra vLLM args."""
    overrides = []

    # FP8 quantization
    if experiment.get("precision") == "FP8":
        overrides.append('decode.containers[name="vllm"].args=--quantization=fp8')

    # GPU memory utilization
    if experiment.get("gpu_mem") and experiment["gpu_mem"] != 0.9:
        overrides.append(f'decode.containers[name="vllm"].args=--gpu-memory-utilization={experiment["gpu_mem"]}')

    # KV cache offloading
    if experiment.get("cpu_offload") or experiment.get("kv_offload"):
        overrides.append(f'decode.containers[name="vllm"].args=--kv-offloading-size={DEFAULT_KV_OFFLOAD_GB}')
        overrides.append('decode.containers[name="vllm"].args=--disable-hybrid-kv-cache-manager')

    # Expert parallelism for MoE models
    dp = experiment.get("dp")
    if dp and dp > 1 and experiment["model"] in MOE_MODELS:
        overrides.append('decode.containers[name="vllm"].args=--enable-expert-parallel')

    # Block size
    if "block_size" in experiment:
        overrides.append(f'decode.containers[name="vllm"].args=--block-size={experiment["block_size"]}')

    # Prefix caching
    if "enable_prefix_caching" in experiment:
        if experiment["enable_prefix_caching"]:
            overrides.append('decode.containers[name="vllm"].args=--enable-prefix-caching')
        else:
            overrides.append('decode.containers[name="vllm"].args=--no-enable-prefix-caching')

    # Chunked prefill
    if experiment.get("enable_chunked_prefill"):
        overrides.append('decode.containers[name="vllm"].args=--enable-chunked-prefill')

    # Priority scheduling
    if experiment.get("scheduling") == "priority":
        overrides.append('decode.containers[name="vllm"].args=--scheduling-policy=priority')

    return overrides


def generate_values_yaml(experiment, clusters, workload_file, workload_data, base_values_path):
    """Generate values.yaml for tektonc compilation using blis-campaign structure.

    Args:
        experiment: Experiment dict from experiment.json
        clusters: Clusters dict from clusters.yaml
        workload_file: Path to workload YAML file
        workload_data: Loaded workload YAML data (with updated trace_rate)
        base_values_path: Path to base values template

    Returns:
        Values dict for YAML serialization

    Raises:
        KeyError: If hw not found in configs
    """
    model_name = experiment["model"]
    hw = experiment["hw"]

    # Validate model ID format (must contain /)
    if "/" not in model_name:
        raise ValueError(f"Model '{model_name}' must be a full HuggingFace ID (org/model)")

    # Validate hardware exists
    if hw not in clusters:
        raise KeyError(f"Hardware {hw} not found in clusters.yaml")

    # Load base values template
    base_values = load_yaml(base_values_path)
    v = copy.deepcopy(base_values)

    # Use model ID directly from experiment.json
    precision = experiment.get("precision", "BF16")
    hf_id = model_name

    # Experiment identity
    exp_name = make_dns_name(f"blis-{experiment['id']}-{model_name}")
    v["experiment"]["name"] = exp_name
    v["experiment"]["description"] = (
        f"Saturation Exp #{experiment['id']}: {model_name} {precision} "
        f"TP{experiment.get('tp', 1)} on {hw}"
    )

    # Stack config
    v["stack"]["MAX_NUM_BATCHED_TOKENS"] = experiment.get("mbt", 2048)
    if "max_model_len" in experiment:
        v["stack"]["MAX_MODEL_LEN"] = experiment["max_model_len"]
    if "max_num_seqs" in experiment:
        v["stack"]["MAX_NUM_SEQS"] = experiment["max_num_seqs"]
    if "block_size" in experiment:
        v["stack"]["BLOCK_SIZE"] = experiment["block_size"]

    v["stack"]["treatments"]["tensorParallelism"] = [experiment.get("tp", 1)]
    v["stack"]["treatments"]["dataLocalParallelism"] = [experiment.get("dp", 1) if experiment.get("dp") else 1]

    # CPU offload
    v["stack"]["cpu_offload"] = experiment.get("cpu_offload", experiment.get("kv_offload", False))
    v["stack"]["kv_offloading_size"] = DEFAULT_KV_OFFLOAD_GB if v["stack"]["cpu_offload"] else 0

    # GPU targeting
    cluster = clusters[hw]
    v["stack"]["model"]["helmValues"]["decode"]["acceleratorTypes"]["labelValues"] = [
        cluster["gpu_label_value"]
    ]

    # GPU reaper exclusion
    decode = v["stack"]["model"]["helmValues"]["decode"]
    if "annotations" not in decode:
        decode["annotations"] = {}
    decode["annotations"]["gpu-reaper.io/exclude"] = "true"

    # Build extra_overrides
    v["stack"]["extra_overrides"] = build_extra_overrides(experiment)

    # Workload - use the loaded workload data
    harness = experiment.get("harness", "orc")
    if harness in ["orc", "blis-orc"]:
        v["workload"]["orcSpec"] = workload_data
        v["workload"]["horizon"] = 600
        # Enable saturation detection for saturation experiments
        v["workload"]["detectSaturation"] = "true"
    else:
        raise ValueError(f"Harness '{harness}' not supported for saturation experiments (only orc/blis-orc)")

    # ORC config
    if "orc" not in v:
        v["orc"] = {}
    v["orc"]["latency_model"] = "trained-physics"
    v["orc"]["gpu_type"] = hw

    return v


# Template mapping
TEMPLATE_MAP = {
    "orc": "tektoncsample/blis-orc/data_pipeline.yaml.j2",
    "blis-orc": "tektoncsample/blis-orc/data_pipeline.yaml.j2",
    "inference-perf": "tektoncsample/blis-inference-perf/data_pipeline.yaml.j2",
}


def compile_pipeline(harness, exp_dir):
    """Compile Tekton pipeline YAML using tektonc.

    Args:
        harness: Harness type (orc, inference-perf)
        exp_dir: Path to experiment directory

    Raises:
        ValueError: If harness not recognized
        RuntimeError: If tektonc compilation fails
    """
    if harness not in TEMPLATE_MAP:
        raise ValueError(
            f"Unknown harness '{harness}'. "
            f"Valid options: {', '.join(TEMPLATE_MAP.keys())}"
        )

    template = TEMPLATE_MAP[harness]
    values_file = exp_dir / "values.yaml"
    output_file = exp_dir / "pipeline.yaml"

    # Call tektonc
    cmd = [
        "python",
        "tektonc/tektonc.py",
        "-t", template,
        "-f", str(values_file),
        "-o", str(output_file),
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"tektonc compilation failed (exit {result.returncode}):\n"
            f"{result.stderr}"
        )


PIPELINERUN_TEMPLATE = """\
apiVersion: tekton.dev/v1
kind: PipelineRun
metadata:
  name: {name}
spec:
  timeouts:
    pipeline: 6h
    tasks: 5h30m
  taskRunTemplate:
    serviceAccountName: helm-installer
  pipelineRef:
    name: {pipeline_name}
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
          - key: ACCESS_KEY
            path: ACCESS_KEY
          - key: SECRET_KEY
            path: SECRET_KEY
"""


def generate_pipelinerun(exp_dir, exp_id):
    """Generate PipelineRun YAML for experiment.

    Args:
        exp_dir: Path to experiment directory
        exp_id: Experiment ID number

    Raises:
        FileNotFoundError: If pipeline.yaml not found
    """
    pipeline_file = exp_dir / "pipeline.yaml"
    if not pipeline_file.exists():
        raise FileNotFoundError(
            f"pipeline.yaml not found in {exp_dir}. "
            "Run compile_pipeline first."
        )

    # Read pipeline name from pipeline.yaml
    pipeline_data = load_yaml(pipeline_file)
    pipeline_name = pipeline_data["metadata"]["name"]

    # Generate unique PipelineRun name with timestamp
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    pr_name = f"saturation-exp{exp_id}-{timestamp}"

    # Generate PipelineRun YAML
    pr_yaml = PIPELINERUN_TEMPLATE.format(
        name=pr_name,
        pipeline_name=pipeline_name
    )

    # Write to file
    pipelinerun_file = exp_dir / "pipelinerun.yaml"
    pipelinerun_file.write_text(pr_yaml)


def process_experiment(exp_name, base_dir, clusters, base_values_path):
    """Process a single saturation experiment.

    Args:
        exp_name: Experiment folder name (e.g., "exp1")
        base_dir: Base directory containing experiment folders
        clusters: Clusters config dict
        base_values_path: Path to base values template

    Returns:
        Tuple of (success: bool, error: str or None)
    """
    exp_dir = base_dir / exp_name

    try:
        # 1. Load experiment config
        experiment = load_json(exp_dir / "experiment.json")

        # 2. Load saturation results
        sat_results = load_json(exp_dir / "saturation_results.json")
        saturation_rps = sat_results["result"]["saturation_point_rps"]

        # 3. Find and load workload file
        workload_file = find_workload_file(exp_dir)
        workload_data = load_yaml(workload_file)

        # 4. Update workload trace_rate
        updated_workload = update_workload_trace_rate(workload_data, saturation_rps)
        write_yaml(workload_file, updated_workload)

        # 5. Generate values.yaml
        values = generate_values_yaml(
            experiment, clusters, workload_file,
            updated_workload, base_values_path
        )
        write_yaml(exp_dir / "values.yaml", values)

        # 6. Compile pipeline
        harness = experiment.get("harness", "orc")
        compile_pipeline(harness, exp_dir)

        # 7. Generate PipelineRun
        generate_pipelinerun(exp_dir, experiment["id"])

        return True, None

    except FileNotFoundError as e:
        return False, f"Missing file: {e}"
    except KeyError as e:
        return False, f"Missing required field: {e}"
    except ValueError as e:
        return False, f"Validation error: {e}"
    except RuntimeError as e:
        return False, f"Compilation error: {e}"
    except Exception as e:
        return False, f"Unexpected error: {e}"


def main():
    """Main entry point."""
    args = parse_args()

    # Determine base directory
    script_dir = Path(__file__).parent
    base_dir = script_dir  # saturation_exps/

    # Load shared configs
    config_dir = script_dir.parent / "blis-campaign" / "config"
    template_dir = script_dir.parent / "tektoncsample"

    try:
        clusters = load_yaml(config_dir / "clusters.yaml")
        # Load base values template for ORC (default harness for saturation experiments)
        base_values_path = template_dir / "blis-orc" / "values.yaml"
    except FileNotFoundError as e:
        print(f"ERROR: Config file not found: {e}")
        return 1

    # Process each experiment
    results = []
    for exp_name in args.experiments:
        print(f"\nProcessing {exp_name}...")

        # Check if experiment folder exists
        exp_dir = base_dir / exp_name
        if not exp_dir.is_dir():
            print(f"  ERROR: Experiment folder not found: {exp_dir}")
            results.append((exp_name, False, "Folder not found"))
            continue

        # Process experiment
        success, error = process_experiment(exp_name, base_dir, clusters, base_values_path)
        results.append((exp_name, success, error))

        if success:
            print(f"  ✓ Generated pipeline files in {exp_dir}")
        else:
            print(f"  ✗ Failed: {error}")

    # Print summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    succeeded = [r for r in results if r[1]]
    failed = [r for r in results if not r[1]]

    print(f"Processed {len(results)} experiments: "
          f"{len(succeeded)} succeeded, {len(failed)} failed")

    if failed:
        print("\nFailed experiments:")
        for exp_name, _, error in failed:
            print(f"  - {exp_name}: {error}")

    # Exit code: 0 if any succeeded, 1 if all failed
    return 0 if succeeded else 1


if __name__ == "__main__":
    exit(main())
