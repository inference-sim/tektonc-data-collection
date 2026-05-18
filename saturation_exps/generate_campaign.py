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


if __name__ == "__main__":
    args = parse_args()
    print(f"Processing experiments: {args.experiments}")
