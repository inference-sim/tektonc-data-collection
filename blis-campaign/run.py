"""GPU-aware single-cluster campaign runner."""
import json
import logging
import re
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

from cluster import run_cmd, kubectl_json, get_available_gpus, get_campaign_gpu_usage, preflight_check
from generate import load_yaml
from state import CampaignState, iso_now
# NOTE: download.py removed - manual download via kubectl (see blis_orc_scripts/LOCAL_REPLAY_README.md)
from cleanup import cleanup_pipeline_run, collect_diagnostics

log = logging.getLogger("blis-campaign")

POLL_INTERVAL = 30  # seconds between polling cycles
STALL_TIMEOUT = 10800  # 3 hours without progress = stall (large model downloads can take 1-2h)
MAX_ATTEMPTS = 2

_draining = False


# ---------------------------------------------------------------------------
# PVC directory name helpers (copied from generate.py)
# ---------------------------------------------------------------------------


def make_dns_name(s):
    """Convert string to DNS-1123 compatible name."""
    s = s.lower()
    s = re.sub(r'[^a-z0-9-]', '-', s)
    s = re.sub(r'-+', '-', s)
    s = s.strip('-')
    return s[:63]


def make_experiment_id(exp):
    """e.g. '68-llama-3-1-8b-tp1-general-lite' -- used as base for PVC data path."""
    return make_dns_name(f"{exp['id']}-{exp['model']}-tp{exp['tp']}-{exp['workload']}")


def make_pvc_dir(exp):
    """Construct PVC directory name: {experiment_id}-{tp}-{dlp}"""
    experiment_id = make_experiment_id(exp)
    tp = exp.get("tp", 1)
    dlp = exp.get("dp", 1) if exp.get("dp") is not None else 1
    return f"{experiment_id}-{tp}-{dlp}"


def _sigint_handler(signum, frame):
    """Graceful shutdown: first Ctrl-C drains, second force-exits."""
    global _draining
    if _draining:
        # Second Ctrl-C: restore default handler and raise
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        raise KeyboardInterrupt
    _draining = True
    log.warning(
        "SIGINT received: draining. No new experiments will launch. "
        "Running experiments will complete. Press Ctrl-C again to force exit."
    )


# ---------------------------------------------------------------------------
# Argument parsing helpers
# ---------------------------------------------------------------------------


def parse_range(range_str):
    """Parse '13-35' into (13, 35) tuple."""
    parts = range_str.split("-")
    if len(parts) != 2:
        raise ValueError(f"Invalid range format '{range_str}', expected 'LO-HI' (e.g. 13-35)")
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(f"Invalid range '{range_str}', both values must be integers")


def parse_only(only_str):
    """Parse '13,25,30' into set of ints."""
    return {int(x.strip()) for x in only_str.split(",") if x.strip()}


# ---------------------------------------------------------------------------
# Experiment filtering
# ---------------------------------------------------------------------------


def _numeric_sort_key(d):
    """Sort directories by leading numeric ID (e.g. '13-qwen3...' sorts as 13)."""
    m = re.match(r'(\d+)', d.name)
    return int(m.group(1)) if m else float('inf')


def filter_experiments(campaign_dir, hw, id_range=None, only_ids=None):
    """Return experiment directories matching the filters, sorted by numeric ID."""
    exp_dirs = sorted(Path(campaign_dir).iterdir(), key=_numeric_sort_key)
    result = []
    for d in exp_dirs:
        if not d.is_dir() or not (d / "experiment.json").exists():
            continue
        exp = json.loads((d / "experiment.json").read_text())

        # Filter by HW
        if exp["hw"] != hw:
            continue
        # Filter by ID range
        if id_range:
            lo, hi = id_range
            if not (lo <= exp["id"] <= hi):
                continue
        # Filter by specific IDs
        if only_ids and exp["id"] not in only_ids:
            continue

        result.append(d)
    return result


# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------


def deploy(exp_dir, context, namespace, attempt):
    """Deploy pipeline + pipelinerun to cluster. Returns pipeline run name."""
    exp = json.loads((exp_dir / "experiment.json").read_text())

    # Apply pipeline
    run_cmd(
        f"kubectl apply -f {exp_dir / 'pipeline.yaml'}",
        context=context,
        namespace=namespace,
    )

    # Stamp unique pipelinerun name and apply via stdin
    name = f"blis-{exp['id']}-attempt{attempt}-{int(time.time())}"
    pr_yaml = (exp_dir / "pipelinerun.yaml").read_text()
    pr_yaml = pr_yaml.replace("__PIPELINE_RUN_NAME__", name)

    proc = subprocess.run(
        f"kubectl apply --context={context} -n {namespace} -f -",
        shell=True,
        input=pr_yaml,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"kubectl apply failed: {proc.stderr}")

    return name


# ---------------------------------------------------------------------------
# Pipeline status monitoring
# ---------------------------------------------------------------------------


def find_running_task(pr_data):
    """Find the currently running task from PipelineRun status."""
    task_runs = pr_data.get("status", {}).get("childReferences", [])
    if task_runs:
        return task_runs[-1].get("pipelineTaskName", "unknown")
    return None


def find_failed_task(pr_data):
    """Find which task failed in the PipelineRun."""
    conditions = pr_data.get("status", {}).get("conditions", [])
    if conditions:
        message = conditions[0].get("message", "")
        return message
    return "unknown"


def check_pipeline_status(pr_name, context, namespace):
    """Poll pipeline run status.

    Returns (status, current_task, failure_reason) where status is one of:
    'Succeeded', 'Failed', 'Running', 'Unknown'
    """
    try:
        data = kubectl_json(
            f"get pipelinerun {pr_name}",
            context=context,
            namespace=namespace,
        )
    except RuntimeError:
        return "Unknown", None, "Failed to query pipeline run"

    conditions = data.get("status", {}).get("conditions", [])
    if not conditions:
        return "Running", None, None

    cond = conditions[0]
    status = cond.get("status", "Unknown")
    reason = cond.get("reason", "")
    message = cond.get("message", "")

    if status == "True":
        return "Succeeded", None, None
    elif status == "False":
        failed_task = find_failed_task(data)
        return "Failed", failed_task, f"{reason}: {message}"
    else:
        current = find_running_task(data)
        return "Running", current, None


# ---------------------------------------------------------------------------
# Success / failure handlers
# ---------------------------------------------------------------------------


def handle_success(dir_name, run_info, state, context, namespace, campaign_dir):
    """Download results automatically and mark completed."""
    log.info(f"SUCCEEDED {dir_name}")

    # Get experiment info for download
    exp_dir = Path(campaign_dir) / dir_name
    exp = json.loads((exp_dir / "experiment.json").read_text())
    exp_id = exp["id"]

    # Use same PVC directory naming as generate.py (DNS-1123 formatted)
    pvc_dir = make_pvc_dir(exp)

    # Create data directory
    data_dir = exp_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Download data using kubectl + tar pipe
    log.info(f"📥 Downloading observe data from PVC...")
    tar_cmd = (
        f"kubectl exec -n {namespace} deployment/busybox --context={context} -- "
        f"tar czf - -C /data {pvc_dir} | tar xzf - -C {data_dir}"
    )

    try:
        result = subprocess.run(tar_cmd, shell=True, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            log.error(f"Download failed for {dir_name}: {result.stderr}")
            log.info(f"Manual download command:")
            log.info(f"  {tar_cmd}")
            state.set_status(dir_name, "completed", completed_at=iso_now(), download_status="failed")
        else:
            log.info(f"✅ Downloaded observe data to {data_dir}/{pvc_dir}")

            # Fix empty header.yaml if needed (workaround for duplicate run overwrite bug)
            header_path = data_dir / pvc_dir / "observe" / "header.yaml"
            if header_path.exists() and header_path.stat().st_size == 0:
                log.info(f"📝 Fixing empty header.yaml (workaround for duplicate run bug)")
                header_path.write_text("{}\n")

            state.set_status(dir_name, "completed", completed_at=iso_now())
            log.info(f"")
            log.info(f"🔄 Next steps (run locally):")
            log.info(f"   python blis_orc_scripts/replay.py --experiment-ids {exp_id}")
            log.info(f"   python blis_orc_scripts/calibrate.py --experiment-ids {exp_id}")
            log.info(f"")
    except subprocess.TimeoutExpired:
        log.error(f"Download timed out for {dir_name} (>5min)")
        state.set_status(dir_name, "completed", completed_at=iso_now(), download_status="timeout")
    except Exception as e:
        log.error(f"Download error for {dir_name}: {e}")
        state.set_status(dir_name, "completed", completed_at=iso_now(), download_status="error")

    # Cleanup PipelineRun (best-effort)
    cleanup_pipeline_run(run_info["pr_name"], context, namespace)


def handle_failure(
    dir_name, run_info, state, reason, pending, context, namespace, campaign_dir
):
    """Collect diagnostics, retry once or mark failed."""
    exp_dir = Path(campaign_dir) / dir_name
    exp = json.loads((exp_dir / "experiment.json").read_text())
    attempts = state.get(dir_name).get("attempts", 1)

    log.error(
        f"FAILED #{exp['id']} {exp['model']} {exp['hw']} {exp['workload']}: "
        f"{reason} (attempt {attempts}/{MAX_ATTEMPTS})"
    )

    # Collect diagnostics
    collect_diagnostics(exp_dir, exp, run_info["pr_name"], context, namespace)

    # Cleanup failed pipeline run
    cleanup_pipeline_run(run_info["pr_name"], context, namespace)

    if attempts < MAX_ATTEMPTS:
        log.info(f"  -> Retrying #{exp['id']}...")
        state.set_status(dir_name, "retrying", last_failure=reason)
        pending.append(exp_dir)
    else:
        log.error(f"  -> Skipping #{exp['id']} after {MAX_ATTEMPTS} attempts")
        state.set_status(dir_name, "failed", last_failure=reason)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def setup_logging(campaign_dir):
    """Configure dual logging: console + file."""
    log_file = Path(campaign_dir) / "campaign.log"
    fmt = "%(asctime)s %(levelname)s %(message)s"

    root = logging.getLogger("blis-campaign")
    root.setLevel(logging.INFO)

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(fmt))
    root.addHandler(console)

    # File handler (append mode for crash resilience)
    file_handler = logging.FileHandler(log_file, mode="a")
    file_handler.setFormatter(logging.Formatter(fmt))
    root.addHandler(file_handler)


# ---------------------------------------------------------------------------
# Campaign summary
# ---------------------------------------------------------------------------


def print_campaign_summary(state, campaign_dir):
    """Print and save final campaign summary."""
    experiments = state.data.get("experiments", {})

    counts = {}
    for entry in experiments.values():
        s = entry.get("status", "pending")
        counts[s] = counts.get(s, 0) + 1

    total = len(experiments)
    completed = counts.get("completed", 0)
    failed = counts.get("failed", 0) + counts.get("download_failed", 0)

    summary = []
    summary.append(f"\n{'=' * 50}")
    summary.append("Campaign Summary")
    summary.append(f"{'=' * 50}")
    summary.append(f"  Total:     {total}")
    summary.append(f"  Completed: {completed}")
    summary.append(f"  Failed:    {failed}")
    for s, c in sorted(counts.items()):
        summary.append(f"  {s:20s} {c}")
    summary.append(f"{'=' * 50}")

    text = "\n".join(summary)
    log.info(text)

    # Save summary file
    summary_path = Path(campaign_dir) / "campaign-summary.txt"
    summary_path.write_text(text)


# ---------------------------------------------------------------------------
# Main scheduler loop
# ---------------------------------------------------------------------------


def find_orphans_count(state):
    """Quick check if any experiments are in orphan-like states."""
    return sum(
        1 for e in state.data.get("experiments", {}).values()
        if e.get("status") in {"running", "deploying", "downloading"}
    )


def run_campaign(args):
    """Main entry point for 'blis-campaign run'."""
    config_dir = Path(__file__).parent / "config"
    clusters = load_yaml(config_dir / "clusters.yaml")

    if args.hw not in clusters:
        print(
            f"ERROR: Unknown hardware type '{args.hw}'. "
            f"Valid: {[k for k in clusters if k != 'namespace']}",
            file=sys.stderr,
        )
        return 1

    cluster = clusters[args.hw]
    context = cluster["context"]
    namespace = clusters["namespace"]

    campaign_dir = Path(args.campaign)
    setup_logging(campaign_dir)

    max_gpus = args.max_gpus
    max_concurrent = args.max_concurrent

    log.info("BLIS Campaign Runner starting")
    log.info(f"  Target: {args.hw} ({context})")
    log.info(f"  Campaign: {campaign_dir}")
    if max_gpus:
        log.info(f"  Max GPUs: {max_gpus}")
    log.info(f"  Max concurrent PipelineRuns: {max_concurrent}")

    # Pre-flight checks
    try:
        preflight_check(args.hw, clusters)
    except RuntimeError as e:
        log.error(f"Pre-flight check failed: {e}")
        return 1

    # Load experiments matching filters
    id_range = parse_range(args.id_range) if args.id_range else None
    only_ids = parse_only(args.only) if args.only else None
    exp_dirs = filter_experiments(campaign_dir, args.hw, id_range, only_ids)

    if not exp_dirs:
        log.error("No experiments match the specified filters")
        return 1

    state = CampaignState(campaign_dir)
    state.mark_started()

    # Install graceful-shutdown handler
    global _draining
    _draining = False
    signal.signal(signal.SIGINT, _sigint_handler)

    # Build pending queue (skip terminal experiments)
    skip_statuses = {"completed", "download_failed", "skipped"}
    pending = deque(
        d for d in exp_dirs if state.get(d.name)["status"] not in skip_statuses
    )
    running = {}  # dir_name -> {pr_name, gpus, started_at, last_task, last_change}

    # Recover orphaned experiments from a previous interrupted run
    from harvest import recover_in_flight
    recovered = recover_in_flight(state, context, namespace, str(campaign_dir), pending, running)
    if recovered or find_orphans_count(state):
        # Rebuild pending to avoid duplicates after recovery
        skip_after = {"completed", "download_failed", "skipped", "failed"}
        pending = deque(
            d for d in exp_dirs
            if state.get(d.name)["status"] not in skip_after
            and d.name not in running
        )

    log.info(f"Campaign: {len(pending)} pending, {len(running)} running, {len(exp_dirs)} total for {args.hw}")

    while pending or running:
        # Drain mode: stop launching, wait for running to finish
        if _draining and not running:
            log.info("Drain complete: all running experiments finished.")
            break
        # Launch phase: skip if draining
        if not _draining:
            # Query GPU availability
            try:
                available = get_available_gpus(
                    context, cluster["gpu_label_key"], cluster["gpu_label_value"], namespace
                )
            except Exception as e:
                log.warning(f"GPU query failed: {e}, retrying next cycle")
                time.sleep(POLL_INTERVAL)
                continue

            # Cap by --max-gpus (subtract GPUs already used by this campaign)
            pr_names = [r["pr_name"] for r in running.values()]
            campaign_gpus = get_campaign_gpu_usage(context, namespace, pr_names) if pr_names else 0
            if max_gpus is not None:
                available = min(available, max_gpus - campaign_gpus)
            log.info(
                f"Campaign using {campaign_gpus}/{max_gpus} GPUs, "
                f"{len(running)}/{max_concurrent} runs — "
                f"{available} GPUs schedulable"
            )

            # Try to start experiments (order-preserving greedy backfill)
            started = []
            for exp_dir in list(pending):
                if len(running) >= max_concurrent:
                    break
                exp = json.loads((exp_dir / "experiment.json").read_text())
                gpus_needed = exp["tp"] * max(exp.get("dp") or 1, 1)

                if available >= gpus_needed:
                    attempt = state.get(exp_dir.name).get("attempts", 0) + 1
                    try:
                        state.set_status(exp_dir.name, "deploying", attempts=attempt)
                        pr_name = deploy(exp_dir, context, namespace, attempt)
                        running[exp_dir.name] = {
                            "pr_name": pr_name,
                            "gpus": gpus_needed,
                            "started_at": time.time(),
                            "last_task": None,
                            "last_change": time.time(),
                        }
                        state.set_status(
                            exp_dir.name,
                            "running",
                            pipeline_run=pr_name,
                            attempts=attempt,
                            started_at=iso_now(),
                        )
                        available -= gpus_needed
                        started.append(exp_dir)
                        log.info(
                            f"STARTED #{exp['id']} {exp['model']} "
                            f"({gpus_needed} GPU{'s' if gpus_needed > 1 else ''}) "
                            f"-> {pr_name}"
                        )
                    except Exception as e:
                        log.error(f"DEPLOY FAILED #{exp['id']}: {e}")
                        if attempt < MAX_ATTEMPTS:
                            log.info(f"  -> Will retry #{exp['id']} next cycle")
                            state.set_status(
                                exp_dir.name, "retrying",
                                attempts=attempt, last_failure=str(e),
                            )
                        else:
                            state.set_status(
                                exp_dir.name, "failed",
                                attempts=attempt, last_failure=str(e),
                            )
                        started.append(exp_dir)  # remove from pending

            for d in started:
                pending.remove(d)

        # Wait before polling
        time.sleep(POLL_INTERVAL)

        # Poll running experiments
        for dir_name, run_info in list(running.items()):
            status, current_task, reason = check_pipeline_status(
                run_info["pr_name"], context, namespace
            )

            # Track progress for stall detection
            if current_task != run_info["last_task"]:
                run_info["last_task"] = current_task
                run_info["last_change"] = time.time()

            if status == "Succeeded":
                handle_success(
                    dir_name, run_info, state, context, namespace, str(campaign_dir)
                )
                del running[dir_name]

            elif status == "Failed":
                handle_failure(
                    dir_name,
                    run_info,
                    state,
                    reason,
                    pending,
                    context,
                    namespace,
                    str(campaign_dir),
                )
                del running[dir_name]

            elif time.time() - run_info["last_change"] > STALL_TIMEOUT:
                log.warning(
                    f"TIMEOUT {dir_name}: no progress for {STALL_TIMEOUT // 60} min"
                )
                handle_failure(
                    dir_name,
                    run_info,
                    state,
                    f"timeout (no progress {STALL_TIMEOUT // 60} min)",
                    pending,
                    context,
                    namespace,
                    str(campaign_dir),
                )
                del running[dir_name]

            elif current_task:
                log.info(f"  {dir_name}: {current_task} ({status})")

    # Restore default signal handler
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # Print final summary
    print_campaign_summary(state, str(campaign_dir))
    return 0
