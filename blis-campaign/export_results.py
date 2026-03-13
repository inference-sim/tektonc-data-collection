#!/usr/bin/env python3
"""Export completed campaign experiment data to results/ directory.

Copies data from blis-campaign/campaign/<dir>/data/<exp-id>/
to results/<exp-id>/ for each experiment marked completed in campaign-state.json.

Usage:
    python blis-campaign/export_results.py \
        --campaign blis-campaign/campaign \
        --output results/
"""
import argparse
import json
import shutil
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Export completed campaign results")
    parser.add_argument("--campaign", required=True, help="Campaign directory")
    parser.add_argument("--output", default="results/", help="Output results directory")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be copied")
    args = parser.parse_args()

    campaign_dir = Path(args.campaign)
    output_dir = Path(args.output)
    state_path = campaign_dir / "campaign-state.json"

    if not state_path.exists():
        print(f"ERROR: {state_path} not found", file=sys.stderr)
        return 1

    with open(state_path) as f:
        state = json.load(f)

    exported = 0
    skipped = 0
    for dir_name, entry in state["experiments"].items():
        if entry.get("status") != "completed":
            skipped += 1
            continue

        data_dir = campaign_dir / dir_name / "data"
        if not data_dir.exists():
            print(f"  SKIP {dir_name}: no data/ directory")
            skipped += 1
            continue

        # Each data dir contains one subdirectory named by experiment-id
        exp_dirs = [d for d in data_dir.iterdir() if d.is_dir()]
        if not exp_dirs:
            print(f"  SKIP {dir_name}: data/ is empty")
            skipped += 1
            continue

        for exp_dir in exp_dirs:
            dest = output_dir / exp_dir.name
            if dest.exists():
                print(f"  EXISTS {exp_dir.name} — skipping")
                continue

            if args.dry_run:
                print(f"  WOULD COPY {exp_dir} -> {dest}")
            else:
                shutil.copytree(exp_dir, dest)
                print(f"  {exp_dir.name}")
            exported += 1

    action = "Would export" if args.dry_run else "Exported"
    print(f"\n{action} {exported} experiments, skipped {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
