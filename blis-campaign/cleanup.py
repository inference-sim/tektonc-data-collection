"""Pipeline cleanup and failure diagnostics."""
import json
import logging
from pathlib import Path

from cluster import run_cmd, kubectl_json

log = logging.getLogger("blis-campaign")


def cleanup_pipeline_run(pr_name, context, namespace):
    """Delete a PipelineRun resource (best-effort)."""
    log.info(f"Cleaning up PipelineRun {pr_name}")
    run_cmd(
        f"kubectl delete pipelinerun {pr_name}",
        context=context, namespace=namespace, ignore_errors=True,
    )


def collect_diagnostics(exp_dir, exp, pipeline_run, context, namespace):
    """Collect diagnostic data on failure for post-mortem analysis.

    Saves to exp_dir/diagnosis/:
    - pipeline-status.json: Full PipelineRun status
    - events.txt: Kubernetes events from namespace
    - pods.json: Pod status for experiment's pods
    - triage.txt: Automated triage pattern matching
    """
    diag_dir = Path(exp_dir) / "diagnosis"
    diag_dir.mkdir(exist_ok=True)

    # Pipeline run status
    try:
        pr_data = kubectl_json(
            f"get pipelinerun {pipeline_run}", context=context, namespace=namespace
        )
        (diag_dir / "pipeline-status.json").write_text(
            json.dumps(pr_data, indent=2)
        )
    except Exception as e:
        log.warning(f"Could not fetch pipeline status: {e}")

    # Events
    try:
        result = run_cmd(
            f"kubectl get events --sort-by=.lastTimestamp",
            context=context, namespace=namespace, ignore_errors=True,
        )
        (diag_dir / "events.txt").write_text(result.stdout)
    except Exception as e:
        log.warning(f"Could not fetch events: {e}")

    # Pods related to this experiment
    model_label = f"{exp['id']}-{exp['model']}-tp{exp['tp']}-{exp['workload']}"
    try:
        result = run_cmd(
            f"kubectl get pods --field-selector=status.phase!=Succeeded",
            context=context, namespace=namespace, ignore_errors=True,
        )
        (diag_dir / "pods.txt").write_text(result.stdout)
    except Exception as e:
        log.warning(f"Could not fetch pods: {e}")

    # Helm releases
    try:
        result = run_cmd(
            f"helm list",
            context=context, namespace=namespace, ignore_errors=True,
        )
        (diag_dir / "helm-releases.txt").write_text(result.stdout)
    except Exception as e:
        log.warning(f"Could not fetch helm releases: {e}")

    # Automated triage
    triage = run_triage(diag_dir)
    (diag_dir / "triage.txt").write_text(triage)

    log.info(f"Diagnostics saved to {diag_dir}")


# Common failure patterns for automated triage
TRIAGE_PATTERNS = {
    "OOMKilled": "Out of memory — try reducing gpu_mem or increasing TP",
    "ImagePullBackOff": "Container image not available — check vLLM image tag",
    "CrashLoopBackOff": "Container crashing repeatedly — check vLLM logs",
    "Unschedulable": "Not enough resources — wait for GPUs to free up",
    "DeadlineExceeded": "Pipeline timeout — consider increasing timeouts",
    "PipelineRunTimeout": "Pipeline timed out",
    "TaskRunTimeout": "Task timed out — model may be too slow to load",
    "FailedMount": "PVC mount failed — check PVC availability",
    "Forbidden": "Auth error — check RBAC permissions",
    "CreateContainerConfigError": "Container config error — check secrets/configmaps",
    "model not found": "Model not found in PVC — run download step first",
    "CUDA out of memory": "GPU OOM during inference — reduce mbt or increase TP",
    "Connection refused": "Model server not ready — startup probe may have timed out",
}


def run_triage(diag_dir):
    """Match diagnostic files against known failure patterns."""
    triage_lines = []

    for diag_file in diag_dir.iterdir():
        if diag_file.suffix in ('.json', '.txt') and diag_file.name != 'triage.txt':
            try:
                content = diag_file.read_text()
                for pattern, explanation in TRIAGE_PATTERNS.items():
                    if pattern in content:
                        triage_lines.append(
                            f"[{diag_file.name}] {pattern}: {explanation}"
                        )
            except Exception:
                pass

    if not triage_lines:
        return "No known failure patterns matched. Manual investigation required."

    return "\n".join(sorted(set(triage_lines)))


def check_orphaned_helm(context, namespace):
    """Warn about orphaned helm releases that may be holding GPU resources."""
    try:
        result = run_cmd(
            f"helm list --filter 'blis-|exp-'",
            context=context, namespace=namespace, ignore_errors=True,
        )
        lines = result.stdout.strip().split('\n')
        if len(lines) > 1:  # header + at least one release
            releases = lines[1:]
            log.warning(f"Found {len(releases)} potentially orphaned helm releases:")
            for r in releases:
                log.warning(f"  {r}")
            return releases
    except Exception as e:
        log.warning(f"Could not check helm releases: {e}")
    return []
