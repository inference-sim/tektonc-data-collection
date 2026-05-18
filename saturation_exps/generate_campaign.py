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
    args = parse_args()
    print(f"Processing experiments: {args.experiments}")
