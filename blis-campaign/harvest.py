"""Recover orphaned experiments from interrupted campaign runs."""
import json
import logging
import time
from collections import deque
from pathlib import Path

from cleanup import cleanup_pipeline_run
from cluster import preflight_check
# NOTE: download.py removed - manual download now required
from generate import load_yaml
from run import (
    check_pipeline_status,
    handle_success,
    handle_failure,
    setup_logging,
    print_campaign_summary,
    make_pvc_dir,
    POLL_INTERVAL,
    MAX_ATTEMPTS,
)
from state import CampaignState, iso_now

log = logging.getLogger("blis-campaign")

ORPHAN_STATUSES = {"running", "deploying"}  # downloading removed - manual download now


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------


def find_orphans(state):
    """Return list of (dir_name, entry) for experiments stuck in active states."""
    orphans = []
    for dir_name, entry in state.data.get("experiments", {}).items():
        if entry.get("status") in ORPHAN_STATUSES:
            orphans.append((dir_name, entry))
    return orphans


def harvest_one(dir_name, entry, state, context, namespace, campaign_dir, pending=None):
    """Resolve one orphaned experiment. Returns a result string.

    Result is one of: "completed", "download_failed", "failed", "retrying", "running".
    """
    status = entry.get("status")
    pr_name = entry.get("pipeline_run")

    # --- downloading: runner died mid-download, retry directly ---
    if status == "downloading":
        return _retry_download(dir_name, state, context, namespace, campaign_dir, pr_name)

    # --- deploying/running with no pipeline_run recorded ---
    if not pr_name:
        log.warning(f"HARVEST {dir_name}: no pipeline_run recorded, marking failed")
        state.set_status(dir_name, "failed", last_failure="no pipeline_run recorded (runner died before deploy completed)")
        return "failed"

    # --- Query the cluster for PipelineRun status ---
    pr_status, current_task, reason = check_pipeline_status(pr_name, context, namespace)

    run_info = {"pr_name": pr_name, "gpus": 0}

    if pr_status == "Succeeded":
        log.info(f"HARVEST {dir_name}: PipelineRun {pr_name} succeeded, downloading results")
        handle_success(dir_name, run_info, state, context, namespace, campaign_dir)
        return "completed" if state.get(dir_name)["status"] == "completed" else "download_failed"

    elif pr_status == "Failed":
        log.warning(f"HARVEST {dir_name}: PipelineRun {pr_name} failed: {reason}")
        if pending is None:
            pending = deque()
        handle_failure(dir_name, run_info, state, reason, pending, context, namespace, campaign_dir)
        return "retrying" if state.get(dir_name)["status"] == "retrying" else "failed"

    elif pr_status == "Running":
        task_info = f" (task: {current_task})" if current_task else ""
        log.info(f"HARVEST {dir_name}: PipelineRun {pr_name} still running{task_info}")
        return "running"

    else:  # Unknown — PipelineRun gone from cluster
        log.warning(f"HARVEST {dir_name}: PipelineRun {pr_name} not found on cluster, marking failed")
        state.set_status(dir_name, "failed", last_failure=f"PipelineRun {pr_name} not found on cluster")
        return "failed"


def _retry_download(dir_name, state, context, namespace, campaign_dir, pr_name):
    """Download observe data automatically for harvested experiments."""
    import subprocess

    log.info(f"HARVEST {dir_name}: downloading observe data")

    # Get experiment info for download
    exp_dir = Path(campaign_dir) / dir_name
    try:
        exp = json.loads((exp_dir / "experiment.json").read_text())
        exp_id = exp["id"]

        # Use same PVC directory naming as generate.py (DNS-1123 formatted)
        pvc_dir = make_pvc_dir(exp)

        # Create data directory
        data_dir = exp_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        # Download using kubectl + tar pipe
        tar_cmd = (
            f"kubectl exec -n {namespace} deployment/busybox --context={context} -- "
            f"tar czf - -C /data {pvc_dir} | tar xzf - -C {data_dir}"
        )

        result = subprocess.run(tar_cmd, shell=True, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            log.error(f"HARVEST {dir_name}: download failed: {result.stderr}")
            state.set_status(dir_name, "completed", completed_at=iso_now(), download_status="failed")
        else:
            log.info(f"HARVEST {dir_name}: download succeeded")

            # Fix empty header.yaml if needed (workaround for duplicate run overwrite bug)
            header_path = data_dir / pvc_dir / "observe" / "header.yaml"
            if header_path.exists() and header_path.stat().st_size == 0:
                log.info(f"HARVEST {dir_name}: fixing empty header.yaml")
                header_path.write_text("{}\n")

            state.set_status(dir_name, "completed", completed_at=iso_now())

    except Exception as e:
        log.error(f"HARVEST {dir_name}: download error: {e}")
        state.set_status(dir_name, "completed", completed_at=iso_now(), download_status="error")

    if pr_name:
        cleanup_pipeline_run(pr_name, context, namespace)
    return "completed"


# ---------------------------------------------------------------------------
# CLI entry point: blis-campaign harvest
# ---------------------------------------------------------------------------


def harvest_campaign(args):
    """Recover orphaned experiments from an interrupted campaign run."""
    config_dir = Path(__file__).parent / "config"
    clusters = load_yaml(config_dir / "clusters.yaml")

    if args.hw not in clusters:
        print(
            f"ERROR: Unknown hardware type '{args.hw}'. "
            f"Valid: {[k for k in clusters if k != 'namespace']}"
        )
        return 1

    cluster = clusters[args.hw]
    context = cluster["context"]
    namespace = clusters["namespace"]

    campaign_dir = Path(args.campaign)
    setup_logging(campaign_dir)

    log.info(f"HARVEST: scanning {campaign_dir} for orphaned experiments")

    state = CampaignState(campaign_dir)
    orphans = find_orphans(state)

    if not orphans:
        log.info("HARVEST: no orphaned experiments found")
        return 0

    log.info(f"HARVEST: found {len(orphans)} orphan(s)")

    still_running = []
    results = {"completed": 0, "download_failed": 0, "failed": 0, "retrying": 0, "running": 0}

    for dir_name, entry in orphans:
        result = harvest_one(dir_name, entry, state, context, namespace, str(campaign_dir))
        results[result] += 1
        if result == "running":
            still_running.append((dir_name, entry))

    # --wait: poll until running experiments finish
    if args.wait and still_running:
        log.info(f"HARVEST: waiting for {len(still_running)} still-running experiment(s)")
        while still_running:
            time.sleep(POLL_INTERVAL)
            remaining = []
            for dir_name, entry in still_running:
                # Re-read state in case it changed
                entry = state.get(dir_name)
                result = harvest_one(dir_name, entry, state, context, namespace, str(campaign_dir))
                if result == "running":
                    remaining.append((dir_name, entry))
                else:
                    results["running"] -= 1
                    results[result] += 1
            still_running = remaining
            if remaining:
                log.info(f"HARVEST: {len(remaining)} experiment(s) still running, polling again in {POLL_INTERVAL}s")

    # Summary
    log.info(
        f"HARVEST complete: "
        f"{results['completed']} completed, "
        f"{results['failed']} failed, "
        f"{results['retrying']} retrying, "
        f"{results['download_failed']} download_failed, "
        f"{results['running']} still running"
    )
    return 0


# ---------------------------------------------------------------------------
# Startup recovery: called by run_campaign()
# ---------------------------------------------------------------------------


def recover_in_flight(state, context, namespace, campaign_dir, pending, running):
    """Recover orphaned experiments at campaign startup.

    Resolves each orphan:
    - Succeeded → handle_success (download results)
    - Failed → handle_failure (with real pending deque for retries)
    - Still Running → add to running dict so the main loop resumes watching
    - Gone / no pr_name → mark failed, re-enqueue if attempts < MAX_ATTEMPTS

    Returns count of experiments added to the running dict.
    """
    orphans = find_orphans(state)
    if not orphans:
        return 0

    log.info(f"RECOVERY: found {len(orphans)} in-flight experiment(s) from previous run")
    added_to_running = 0

    for dir_name, entry in orphans:
        pr_name = entry.get("pipeline_run")
        status = entry.get("status")

        # downloading: retry download directly
        if status == "downloading":
            _retry_download(dir_name, state, context, namespace, campaign_dir, pr_name)
            continue

        # No pipeline_run recorded
        if not pr_name:
            log.warning(f"RECOVERY {dir_name}: no pipeline_run, marking failed")
            state.set_status(dir_name, "failed", last_failure="no pipeline_run (runner died before deploy)")
            attempts = entry.get("attempts", 1)
            if attempts < MAX_ATTEMPTS:
                exp_dir = Path(campaign_dir) / dir_name
                if exp_dir.is_dir():
                    state.set_status(dir_name, "retrying", last_failure="no pipeline_run (runner died before deploy)")
                    pending.append(exp_dir)
                    log.info(f"RECOVERY {dir_name}: re-enqueued for retry")
            continue

        # Query cluster
        pr_status, current_task, reason = check_pipeline_status(pr_name, context, namespace)
        run_info = {"pr_name": pr_name, "gpus": 0}

        if pr_status == "Succeeded":
            log.info(f"RECOVERY {dir_name}: PipelineRun succeeded, downloading")
            handle_success(dir_name, run_info, state, context, namespace, campaign_dir)

        elif pr_status == "Failed":
            log.warning(f"RECOVERY {dir_name}: PipelineRun failed: {reason}")
            handle_failure(dir_name, run_info, state, reason, pending, context, namespace, campaign_dir)

        elif pr_status == "Running":
            log.info(f"RECOVERY {dir_name}: PipelineRun still running, resuming watch")
            # Read experiment.json to get GPU count for the running dict
            exp_dir = Path(campaign_dir) / dir_name
            try:
                exp = json.loads((exp_dir / "experiment.json").read_text())
                gpus = exp["tp"] * max(exp.get("dp") or 1, 1)
            except Exception:
                gpus = 1  # conservative fallback
            running[dir_name] = {
                "pr_name": pr_name,
                "gpus": gpus,
                "started_at": time.time(),
                "last_task": current_task,
                "last_change": time.time(),
            }
            added_to_running += 1

        else:  # Unknown — PipelineRun gone
            log.warning(f"RECOVERY {dir_name}: PipelineRun not found, marking failed")
            state.set_status(dir_name, "failed", last_failure=f"PipelineRun {pr_name} not found on cluster")
            attempts = entry.get("attempts", 1)
            if attempts < MAX_ATTEMPTS:
                exp_dir = Path(campaign_dir) / dir_name
                if exp_dir.is_dir():
                    state.set_status(dir_name, "retrying", last_failure=f"PipelineRun {pr_name} gone")
                    pending.append(exp_dir)
                    log.info(f"RECOVERY {dir_name}: re-enqueued for retry")

    log.info(f"RECOVERY complete: {added_to_running} experiment(s) added to running watch list")
    return added_to_running
