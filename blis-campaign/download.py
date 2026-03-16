"""PVC data download via tar pipe + file verification."""
import json
import logging
import re
import subprocess
import time
from pathlib import Path

from cluster import run_cmd
from generate import load_yaml, make_experiment_id

log = logging.getLogger("blis-campaign")

# Files expected in each experiment's data directory
REQUIRED_FILES = [
    "exp-config.yaml",
    "profile.yaml",
    "vllm_logging.json",
    "vllm.log",
    "results/per_request_lifecycle_metrics.json",
    "results/summary_lifecycle_metrics.json",
    "results/config.yaml",
    "results/stdout.log",
    "results/stderr.log",
]
REQUIRED_PATTERNS = ["results/stage_*_lifecycle_metrics.json"]


BUSYBOX_POD_TEMPLATE = """\
apiVersion: v1
kind: Pod
metadata:
  name: {pod_name}
  namespace: {namespace}
spec:
  restartPolicy: Never
  containers:
  - name: busybox
    image: busybox:latest
    command: ["sleep", "3600"]
    volumeMounts:
    - name: data
      mountPath: /data
  volumes:
  - name: data
    persistentVolumeClaim:
      claimName: data-pvc
"""


class DownloadError(Exception):
    pass


def create_busybox_pod(context, namespace):
    """Create a short-lived busybox pod with data-pvc mounted. Returns pod name."""
    pod_name = f"campaign-download-{int(time.time())}"
    manifest = BUSYBOX_POD_TEMPLATE.format(pod_name=pod_name, namespace=namespace)

    proc = subprocess.run(
        f"kubectl apply --context={context} -f -",
        shell=True, input=manifest, capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise DownloadError(f"Failed to create busybox pod: {proc.stderr}")

    # Wait for pod to be ready
    run_cmd(
        f"kubectl wait --for=condition=Ready pod/{pod_name} --timeout=120s",
        context=context, namespace=namespace
    )
    log.info(f"Busybox pod {pod_name} ready")
    return pod_name


def delete_busybox_pod(pod_name, context, namespace):
    """Delete the busybox pod (best-effort)."""
    run_cmd(
        f"kubectl delete pod {pod_name}",
        context=context, namespace=namespace, ignore_errors=True
    )


def pvc_dir_exists(pod_name, remote_path, context, namespace):
    """Check if a directory exists on the PVC via the busybox pod."""
    cmd = (
        f"kubectl exec {pod_name} --context={context} -n {namespace} -- "
        f"test -d /data/{remote_path}"
    )
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.returncode == 0


def tar_copy(pod_name, remote_path, local_dest, context, namespace):
    """Download directory from pod via tar pipe (avoids kubectl cp tar warning bug).

    The tar pipe approach is more reliable than kubectl cp which can corrupt
    files due to a known tar header warning issue.
    """
    local_dest = Path(local_dest)
    local_dest.mkdir(parents=True, exist_ok=True)

    cmd = (
        f"kubectl exec {pod_name} --context={context} -n {namespace} -- "
        f"tar cf - -C /data {remote_path} | tar xf - -C {local_dest}"
    )
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        raise DownloadError("tar pipe timed out after 600s")
    if result.returncode != 0:
        raise DownloadError(f"tar pipe failed: {result.stderr}")

    log.info(f"Downloaded {remote_path} to {local_dest}")


def verify_download(local_dest, experiment_id):
    """Verify required files exist and are non-empty.

    Raises DownloadError if verification fails.
    """
    data_dir = Path(local_dest) / experiment_id
    missing = []
    empty = []

    for f in REQUIRED_FILES:
        fp = data_dir / f
        if not fp.exists():
            missing.append(f)
        elif fp.stat().st_size == 0:
            empty.append(f)

    for pattern in REQUIRED_PATTERNS:
        matches = list(data_dir.glob(pattern))
        if not matches:
            missing.append(pattern)
        else:
            for m in matches:
                if m.stat().st_size == 0:
                    empty.append(str(m.relative_to(data_dir)))

    issues = []
    if missing:
        issues.append(f"Missing files: {', '.join(missing)}")
    if empty:
        issues.append(f"Empty files: {', '.join(empty)}")

    if issues:
        raise DownloadError("; ".join(issues))

    log.info("Download verified: all required files present and non-empty")


def download_and_verify(campaign_dir, dir_name, context, namespace):
    """Orchestrate full download flow: create pod, tar copy, verify, cleanup pod.

    Downloads experiment data from the PVC to campaign_dir/dir_name/data/.
    """
    exp_dir = Path(campaign_dir) / dir_name
    exp = json.loads((exp_dir / "experiment.json").read_text())

    experiment_id = make_experiment_id(exp)
    dp = exp.get("dp") or 1

    # New format (tp-dp): matches template stackModelLabel "$(params.experimentId)-{{ tp }}-{{ dlp }}"
    # Old format (tp only): matches pre-dlp template "$(params.experimentId)-{{ tp }}"
    new_fmt = f"{experiment_id}-{exp['tp']}-{dp}"
    old_fmt = f"{experiment_id}-{exp['tp']}"

    local_dest = exp_dir / "data"

    pod_name = None
    try:
        pod_name = create_busybox_pod(context, namespace)

        # Probe PVC for the correct directory format
        if pvc_dir_exists(pod_name, new_fmt, context, namespace):
            pvc_data_dir = new_fmt
        elif pvc_dir_exists(pod_name, old_fmt, context, namespace):
            log.info(f"Using old PVC path format (no dp suffix): {old_fmt}")
            pvc_data_dir = old_fmt
        else:
            raise DownloadError(
                f"Data directory not found on PVC: tried {new_fmt} and {old_fmt}"
            )

        tar_copy(pod_name, pvc_data_dir, local_dest, context, namespace)
        verify_download(local_dest, pvc_data_dir)
    finally:
        if pod_name:
            delete_busybox_pod(pod_name, context, namespace)


def retry_downloads(args):
    """Retry downloads for experiments stuck in download_failed status."""
    from state import CampaignState, iso_now

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
    state = CampaignState(campaign_dir)

    only_ids = None
    if args.only:
        only_ids = {int(x.strip()) for x in args.only.split(",") if x.strip()}

    # Find experiments in download_failed status matching filters
    targets = []
    for d in sorted(campaign_dir.iterdir(), key=lambda p: _numeric_sort_key(p)):
        if not d.is_dir() or not (d / "experiment.json").exists():
            continue
        exp = json.loads((d / "experiment.json").read_text())
        if exp["hw"] != args.hw:
            continue
        if only_ids and exp["id"] not in only_ids:
            continue
        entry = state.get(d.name)
        if entry["status"] != "download_failed":
            continue
        targets.append(d)

    if not targets:
        print("No download_failed experiments match the specified filters.")
        return 0

    print(f"Retrying downloads for {len(targets)} experiment(s)...")

    succeeded, failed = [], []
    for exp_dir in targets:
        dir_name = exp_dir.name
        print(f"\n  Retrying {dir_name}...")
        state.set_status(dir_name, "downloading")
        try:
            download_and_verify(str(campaign_dir), dir_name, context, namespace)
            state.set_status(dir_name, "completed", completed_at=iso_now())
            print(f"  -> {dir_name}: OK")
            succeeded.append(dir_name)
        except DownloadError as e:
            print(f"  -> {dir_name}: FAILED ({e})")
            state.set_status(dir_name, "download_failed", last_failure=str(e))
            failed.append(dir_name)

    print(f"\nRetry summary: {len(succeeded)} succeeded, {len(failed)} failed")
    return 0 if not failed else 1


def _numeric_sort_key(p):
    """Sort paths by leading numeric ID (e.g. '13-qwen3...' sorts as 13)."""
    m = re.match(r"(\d+)", p.name)
    return int(m.group(1)) if m else float("inf")
