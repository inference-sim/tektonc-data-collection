"""Cluster operations: kubectl wrappers, GPU queries, pre-flight checks."""
import json
import logging
import shutil
import subprocess

log = logging.getLogger("blis-campaign")


def run_cmd(cmd, context=None, namespace=None, timeout=60, ignore_errors=False):
    """Run a shell command, optionally with --context and -n flags.

    Returns subprocess.CompletedProcess.
    """
    if context:
        cmd = f"{cmd} --context={context}"
    if namespace:
        cmd = f"{cmd} -n {namespace}"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0 and not ignore_errors:
        raise RuntimeError(f"Command failed: {cmd}\nstderr: {result.stderr.strip()}")
    return result


def helm_cmd(cmd, context=None, namespace=None, timeout=60, ignore_errors=False):
    """Run a helm command with --kube-context and --namespace flags.

    Helm uses different flag names than kubectl.
    """
    if context:
        cmd = f"{cmd} --kube-context={context}"
    if namespace:
        cmd = f"{cmd} --namespace {namespace}"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0 and not ignore_errors:
        raise RuntimeError(f"Command failed: {cmd}\nstderr: {result.stderr.strip()}")
    return result


def kubectl_json(cmd, context=None, namespace=None, timeout=60):
    """Run kubectl command and parse JSON output."""
    result = run_cmd(f"kubectl {cmd} -o json", context=context, namespace=namespace, timeout=timeout)
    return json.loads(result.stdout)


def get_available_gpus(context, gpu_label_key, gpu_label_value):
    """Query total allocatable GPUs on nodes matching the GPU label.

    Returns count of allocatable GPUs on matching nodes.
    Note: Does not check cluster-wide allocation (requires cluster-scope permissions).
    The campaign runner tracks its own namespace GPU usage via get_campaign_gpu_usage().
    """
    # Get total allocatable GPUs on matching nodes
    nodes = kubectl_json(
        f"get nodes -l {gpu_label_key}={gpu_label_value}", context=context
    )
    total = sum(
        int(n["status"]["allocatable"].get("nvidia.com/gpu", 0))
        for n in nodes.get("items", [])
    )

    log.info(f"Cluster GPUs: {total} allocatable on matching nodes")
    return total


def get_campaign_gpu_usage(context, namespace, pr_names):
    """Query actual GPU usage in the campaign namespace.

    Counts all nvidia.com/gpu requests from running pods in the namespace.
    This includes model-serving pods (Helm Deployments) and Tekton task pods,
    since model-serving pods don't carry tekton.dev/pipelineRun labels.
    """
    pods = kubectl_json(
        "get pods --field-selector=status.phase=Running",
        context=context,
        namespace=namespace,
    )
    gpus = 0
    for p in pods.get("items", []):
        for c in p["spec"].get("containers", []):
            gpus += int(
                c.get("resources", {}).get("requests", {}).get("nvidia.com/gpu", 0)
            )
    return gpus


def preflight_check(hw, clusters):
    """Run pre-flight checks for the target cluster. Raises on failure."""
    cluster = clusters[hw]
    context = cluster["context"]
    namespace = clusters["namespace"]

    log.info(f"Pre-flight checks for {hw} ({context})...")

    # Check required binaries
    for binary in ["kubectl", "tkn", "helm"]:
        if not shutil.which(binary):
            raise RuntimeError(f"Required binary not found: {binary}")
    log.info("  Binaries: kubectl, tkn, helm found")

    # Check cluster reachable (using get nodes instead of cluster-info to avoid kube-system access requirement)
    run_cmd(f"kubectl get nodes --request-timeout=15s", context=context, timeout=20)
    log.info(f"  Cluster {context}: reachable")

    # Check namespace exists
    run_cmd(f"kubectl get namespace {namespace}", context=context)
    log.info(f"  Namespace {namespace}: exists")

    # Check required Tekton tasks (cluster-specific)
    # H100/A100 use regular tasks with StepActions, L40S uses inline tasks
    use_inline = cluster.get("use_inline_tasks", False)
    if use_inline:
        required_tasks = [
            "download-model", "deploy-model-inline", "delete-model-inline",
            "create-exp-config", "install-inference-perf-blis",
            "run-workload-inference-perf-blis", "upload-s3",
        ]
    else:
        required_tasks = [
            "download-model", "deploy-model", "delete-model",
            "create-exp-config", "install-inference-perf-blis",
            "run-workload-inference-perf-blis", "upload-s3",
        ]
    result = run_cmd(f"kubectl get tasks -n {namespace}", context=context, ignore_errors=True)
    missing = []
    for task in required_tasks:
        if task not in result.stdout:
            missing.append(task)
    if missing:
        log.warning(f"  Missing Tekton tasks: {', '.join(missing)}")
    else:
        log.info(f"  Tekton tasks: all {len(required_tasks)} required tasks found")

    # Check GPU availability
    available = get_available_gpus(context, cluster["gpu_label_key"], cluster["gpu_label_value"])
    log.info(f"  GPUs available: {available}")

    # Check auth
    run_cmd(
        f"kubectl auth can-i create pipelinerun -n {namespace}",
        context=context,
    )
    log.info(f"  Auth: can create pipelineruns")

    log.info(f"Pre-flight checks passed for {hw}")
