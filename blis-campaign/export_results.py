#!/usr/bin/env python3
"""Export completed campaign experiment data to results/ directory.

Copies data from blis-campaign/campaign/<dir>/data/<exp-id>/
to results/<exp-id>/ for each experiment marked completed in campaign-state.json.

Handles both old (exp-id-tp) and new (exp-id-tp-dp) data directory formats
by reading experiment.json to determine the correct path.

Usage:
    python blis-campaign/export_results.py \
        --campaign blis-campaign/campaign \
        --output results/
"""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path


def _make_dns_name(s):
    """Convert string to DNS-1123 compatible name."""
    s = s.lower()
    s = re.sub(r'[^a-z0-9-]', '-', s)
    s = re.sub(r'-+', '-', s)
    s = s.strip('-')
    return s[:63]


def _expected_data_dir(exp):
    """Build the expected data directory name from experiment config.

    New format (tp-dp): e.g. 33-llama-4-scout-17b-16e-tp2-general-2-2
    Old format (tp only): e.g. 33-llama-4-scout-17b-16e-tp2-general-2
    """
    experiment_id = _make_dns_name(
        f"{exp['id']}-{exp['model']}-tp{exp['tp']}-{exp['workload']}"
    )
    dp = exp.get("dp") or 1
    return f"{experiment_id}-{exp['tp']}-{dp}"


def _find_data_dir(data_dir, exp):
    """Find the correct data subdirectory, trying new format first then old."""
    # New format: exp-id-tp-dp
    new_name = _expected_data_dir(exp)
    new_path = data_dir / new_name
    if new_path.is_dir():
        return new_path

    # Old format: exp-id-tp (no dp suffix)
    experiment_id = _make_dns_name(
        f"{exp['id']}-{exp['model']}-tp{exp['tp']}-{exp['workload']}"
    )
    old_name = f"{experiment_id}-{exp['tp']}"
    old_path = data_dir / old_name
    if old_path.is_dir():
        return old_path

    return None


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

        exp_json_path = campaign_dir / dir_name / "experiment.json"
        data_dir = campaign_dir / dir_name / "data"
        if not data_dir.exists():
            print(f"  SKIP {dir_name}: no data/ directory")
            skipped += 1
            continue

        if not exp_json_path.exists():
            print(f"  SKIP {dir_name}: no experiment.json")
            skipped += 1
            continue

        exp = json.loads(exp_json_path.read_text())
        exp_dir = _find_data_dir(data_dir, exp)

        if exp_dir is None:
            print(f"  SKIP {dir_name}: no matching data subdirectory")
            skipped += 1
            continue

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
