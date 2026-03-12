"""PVC data download via tar pipe + file verification."""
import logging
import subprocess
import time
from pathlib import Path

from cluster import run_cmd
from generate import make_experiment_id

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
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=600)
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
    import json

    exp_dir = Path(campaign_dir) / dir_name
    exp = json.loads((exp_dir / "experiment.json").read_text())

    experiment_id = make_experiment_id(exp)

    local_dest = exp_dir / "data"

    pod_name = None
    try:
        pod_name = create_busybox_pod(context, namespace)
        tar_copy(pod_name, experiment_id, local_dest, context, namespace)
        verify_download(local_dest, experiment_id)
    finally:
        if pod_name:
            delete_busybox_pod(pod_name, context, namespace)
