#!/usr/bin/env python3
"""BLIS Campaign Runner CLI."""
import argparse
import sys
import os

# Add this directory to sys.path so sibling module imports work
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    parser = argparse.ArgumentParser(prog="blis-campaign",
                                     description="BLIS Campaign Runner")
    sub = parser.add_subparsers(dest="command", required=True)

    # generate
    gen = sub.add_parser("generate", help="Generate experiment pipeline YAML")
    gen.add_argument("--experiments", required=True, help="Path to experiments.json")
    gen.add_argument("--output", default="campaign/", help="Output directory")

    # run
    run = sub.add_parser("run", help="Run experiments on a cluster")
    run.add_argument("--campaign", required=True, help="Campaign directory")
    run.add_argument("--hw", required=True, help="Hardware type (H100, A100-80GB, L40S)")
    run.add_argument("--range", dest="id_range", help="ID range, e.g. 13-35")
    run.add_argument("--only", help="Comma-separated experiment IDs")
    run.add_argument("--max-gpus", type=int, default=16,
                     help="Max GPUs to use concurrently (default: 16)")

    # status
    st = sub.add_parser("status", help="Show campaign status")
    st.add_argument("--campaign", required=True, help="Campaign directory")

    args = parser.parse_args()

    if args.command == "generate":
        from generate import generate_campaign
        return generate_campaign(args)
    elif args.command == "run":
        from run import run_campaign
        return run_campaign(args)
    elif args.command == "status":
        from state import print_status
        return print_status(args)


if __name__ == "__main__":
    raise SystemExit(main())
