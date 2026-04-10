#!/usr/bin/env python3
"""Local BLIS calibrate script.

Runs calibrate phase on downloaded observe + replay data for one or more experiments.
Automatically clones/builds BLIS if needed.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path


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
        matches = list(campaign_path.glob(f"{exp_id}-*"))
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
    exp_json = exp_dir / "experiment.json"
    if not exp_json.exists():
        raise ValueError(f"experiment.json not found in {exp_dir}")

    with open(exp_json) as f:
        exp = json.load(f)

    exp_id = exp["id"]

    # Check canonical campaign data location first
    campaign_data = exp_dir / "data"
    if campaign_data.exists():
        # Find subdirectory matching experiment pattern
        for subdir in campaign_data.iterdir():
            if subdir.is_dir() and (subdir / "observe" / "header.yaml").exists():
                return subdir

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
            return path
        if path.exists() and (path / "header.yaml").exists():
            return path.parent

    raise ValueError(
        f"Downloaded data not found for experiment {exp_id}. "
        f"Expected to find observe/ and replay/ directories. "
        f"Tried campaign data dir: {campaign_data}, legacy paths: {[str(p) for p in possible_paths]}"
    )


def run_calibrate(blis_binary, exp_dir, data_dir):
    """Run BLIS calibrate on observe + replay data."""
    exp_json = exp_dir / "experiment.json"
    with open(exp_json) as f:
        exp = json.load(f)

    observe_dir = data_dir / "observe"
    replay_dir = data_dir / "replay"

    # Verify inputs exist
    if not (observe_dir / "header.yaml").exists():
        raise ValueError(f"Observe header.yaml not found at {observe_dir}")
    if not (observe_dir / "data.csv").exists():
        raise ValueError(f"Observe data.csv not found at {observe_dir}")
    if not (replay_dir / "sim_result.json").exists():
        raise ValueError(
            f"Replay sim_result.json not found at {replay_dir}. "
            f"Run replay first: python blis-campaign/replay.py --experiment-ids {exp['id']}"
        )

    # Create calibrate output directory
    calibrate_dir = data_dir / "calibrate"
    calibrate_dir.mkdir(parents=True, exist_ok=True)

    # Build command (use absolute paths since BLIS runs from repo dir)
    cmd = [
        str(blis_binary), "calibrate",
        "--trace-header", str((observe_dir / "header.yaml").resolve()),
        "--trace-data", str((observe_dir / "data.csv").resolve()),
        "--sim-results", str((replay_dir / "sim_result.json").resolve()),
        "--report", str((calibrate_dir / "calibration_report.json").resolve()),
    ]

    # Add ITL data if available
    itl_csv = observe_dir / "itl.csv"
    if itl_csv.exists():
        cmd.extend(["--itl-data", str(itl_csv.resolve())])

    print(f"\n🔍 Running calibrate for experiment {exp['id']} ({exp['model']} on {exp['hw']})...")
    print(f"   Observe data: {observe_dir}")
    print(f"   Replay result: {replay_dir / 'sim_result.json'}")
    print(f"   Calibrate output: {calibrate_dir}")
    if itl_csv.exists():
        print(f"   ITL data: {itl_csv} (enabled)")
    print(f"   Command: {' '.join(cmd)}")

    # Run calibrate (change to BLIS repo dir so it finds bundled configs)
    result = subprocess.run(
        cmd,
        cwd=blis_binary.parent,
        capture_output=True,
        text=True
    )

    # Save logs
    (calibrate_dir / "stdout.log").write_text(result.stdout)
    (calibrate_dir / "stderr.log").write_text(result.stderr)

    if result.returncode != 0:
        print(f"❌ Calibrate failed for experiment {exp['id']}")
        print(f"   stderr: {result.stderr[:500]}")
        return False

    print(f"✅ Calibrate completed for experiment {exp['id']}")
    print(f"   Output: {calibrate_dir / 'calibration_report.json'}")

    # Print summary if report exists
    report_path = calibrate_dir / "calibration_report.json"
    if report_path.exists():
        try:
            with open(report_path) as f:
                report = json.load(f)
            print(f"   📊 Calibration summary:")
            if "metrics" in report:
                for metric, value in report["metrics"].items():
                    print(f"      {metric}: {value}")
        except Exception as e:
            print(f"   ⚠️  Could not parse calibration report: {e}")

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Run BLIS calibrate locally on downloaded observe + replay data"
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

    args = parser.parse_args()

    # Parse experiment IDs
    exp_ids = [int(x.strip()) for x in args.experiment_ids.split(",")]

    # Ensure BLIS is built
    blis_repo_path = Path(args.blis_repo).resolve()
    blis_binary = ensure_blis_built(blis_repo_path)

    # Find experiment directories
    exp_dirs = find_experiment_dirs(args.campaign, exp_ids)
    if not exp_dirs:
        print("❌ No experiment directories found")
        return 1

    # Run calibrate for each experiment
    successes = 0
    failures = 0

    for exp_dir in exp_dirs:
        try:
            data_dir = get_downloaded_data_dir(exp_dir)
            success = run_calibrate(blis_binary, exp_dir, data_dir)
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
