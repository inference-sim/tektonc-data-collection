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


if __name__ == "__main__":
    exit(main())


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


def generate_values_yaml(experiment, models, clusters, workload_file):
    """Generate values.yaml for tektonc compilation.

    Args:
        experiment: Experiment dict from experiment.json
        models: Models dict from models.yaml
        clusters: Clusters dict from clusters.yaml
        workload_file: Path to workload YAML file

    Returns:
        Values dict for YAML serialization

    Raises:
        KeyError: If model or hw not found in configs
    """
    model_name = experiment["model"]
    hw = experiment["hw"]

    # Validate model exists
    if model_name not in models:
        raise KeyError(f"Model {model_name} not found in models.yaml")

    # Validate hardware exists
    if hw not in clusters:
        raise KeyError(f"Hardware {hw} not found in clusters.yaml")

    model_config = models[model_name]
    cluster_config = clusters[hw]

    # Build values structure
    values = {
        "model": {
            "name": model_name,
            "image": model_config["image"],
            "checkpoint": model_config.get("checkpoint", model_name),
            "tp": experiment.get("tp", 1),
            "dp": experiment.get("dp", 1) if experiment.get("dp") else 1,
        },
        "cluster": {
            "context": cluster_config["context"],
            "namespace": cluster_config["namespace"],
        },
        "workload_file": str(workload_file),
        "harness": experiment.get("harness", "orc"),
        "scheduling": experiment.get("scheduling", "fcfs"),
    }

    # Add optional fields if present
    if "precision" in experiment:
        values["precision"] = experiment["precision"]
    if "gpu_mem" in experiment:
        values["gpu_mem"] = experiment["gpu_mem"]
    if "chunk_size" in experiment:
        values["chunk_size"] = experiment["chunk_size"]

    return values


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


def process_experiment(exp_name, base_dir, models, clusters):
    """Process a single saturation experiment.

    Args:
        exp_name: Experiment folder name (e.g., "exp1")
        base_dir: Base directory containing experiment folders
        models: Models config dict
        clusters: Clusters config dict

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
        values = generate_values_yaml(experiment, models, clusters, workload_file)
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

    try:
        models = load_yaml(config_dir / "models.yaml")
        clusters = load_yaml(config_dir / "clusters.yaml")
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
        success, error = process_experiment(exp_name, base_dir, models, clusters)
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
