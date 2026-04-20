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
    gen.add_argument("--only", help="Comma-separated experiment IDs to regenerate (e.g. 24,29,32)")
    gen.add_argument("--all", action="store_true", default=False,
                     help="Include done experiments (regenerate everything)")

    # run
    run = sub.add_parser("run", help="Run experiments on a cluster")
    run.add_argument("--campaign", required=True, help="Campaign directory")
    run.add_argument("--hw", required=True, help="Hardware type (H100, A100-80GB, L40S)")
    run.add_argument("--range", dest="id_range", help="ID range, e.g. 13-35")
    run.add_argument("--only", help="Comma-separated experiment IDs")
    run.add_argument("--max-gpus", type=int, default=16,
                     help="Max GPUs to use concurrently (default: 16)")
    run.add_argument("--max-concurrent", type=int, default=4,
                     help="Max PipelineRuns running simultaneously (default: 4)")
    run.add_argument("--all", dest="safe_only", action="store_false", default=True,
                     help="Include unsafe/blocked/uncalibrated experiments (default: safe only)")

    # status
    st = sub.add_parser("status", help="Show campaign status")
    st.add_argument("--campaign", required=True, help="Campaign directory")

    # retry-downloads
    rd = sub.add_parser("retry-downloads", help="Retry failed downloads")
    rd.add_argument("--campaign", required=True, help="Campaign directory")
    rd.add_argument("--hw", required=True, help="Hardware type (H100, A100-80GB, L40S)")
    rd.add_argument("--only", help="Comma-separated experiment IDs")

    # harvest
    hv = sub.add_parser("harvest", help="Recover orphaned experiments from interrupted runs")
    hv.add_argument("--campaign", required=True, help="Campaign directory")
    hv.add_argument("--hw", required=True, help="Hardware type (H100, A100-80GB, L40S)")
    hv.add_argument("--wait", action="store_true", default=False,
                     help="Poll until still-running experiments complete")

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
    elif args.command == "retry-downloads":
        from download import retry_downloads
        return retry_downloads(args)
    elif args.command == "harvest":
        from harvest import harvest_campaign
        return harvest_campaign(args)


if __name__ == "__main__":
    raise SystemExit(main())
