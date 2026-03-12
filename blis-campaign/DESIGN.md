# BLIS Campaign Runner — Design Spec

**Date:** 2026-03-11
**Status:** Draft

## Executive Summary

You have 53 experiments to run across three clusters (pokprod001 for H100, fmaas-vllmd for A100, fmaas-platform-eval for L40S). Today you run them one at a time via a Claude skill that generates YAML on the fly — slow, error-prone, no retries, no batch support.

The campaign runner replaces this with two steps:

1. **Generate** — A Python script reads your experiment table (a JSON file matching the [#598 discussion](https://github.com/inference-sim/inference-sim/discussions/598)) and pre-builds all pipeline YAML files for every experiment. You inspect them before anything touches a cluster. No AI involved — just dict lookups and tektonc compilation.

2. **Run** — A second script takes the pre-built files and executes them. Here's how it picks what to run:

   - You configure how many GPUs are available per cluster (e.g., 8 H100s on pokprod001, 8 A100s on fmaas).
   - Each experiment needs `tp * dp` GPUs (e.g., experiment #13 needs 1 H100, experiment #15 needs 8 H100s).
   - The runner walks the experiment list **in table order** and starts each experiment if there are enough free GPUs on its cluster. So if you have 8 H100s, it might start experiments #13 (1 GPU), #14 (1 GPU), #15 (1 GPU), and #16 (1 GPU) all at the same time — that's 4 GPUs in use, 4 free.
   - When an experiment finishes, its GPUs are freed. The runner immediately checks: can the next pending experiment (in table order) fit now? If yes, start it. If no (e.g., it needs 8 GPUs but only 3 are free), look ahead for a smaller experiment that fits.
   - Different clusters run independently — an H100 experiment and an A100 experiment run in parallel on their respective clusters.

   The runner is a **long-running Python process** — not Claude. It polls `tkn pr describe` every 30 seconds to monitor each running experiment, detects success/failure/stalls, downloads results with full verification, cleans up pipeline runs, and moves on. If something fails, it retries once then skips — and prints a clear message to stdout with the experiment ID, failure reason, and which task failed (e.g., `FAILED #15 DeepSeek-V3: deploy-model timed out after 60min (attempt 2/2, skipping)`). If you kill the process, it resumes from where it left off (progress is saved to `campaign-state.json` after every state change).

   You can check progress at any time by running `blis-campaign status`, which reads the state file and prints a table of what's completed, running, pending, and failed.

All experiments run in the `diya` namespace. GPU capacity per cluster is configurable. **Everything is Python — no AI in the loop.** Claude is only a thin wrapper that calls the Python CLI for a nicer interactive experience; it does no YAML generation, monitoring, or decision-making.

**Designed for unattended overnight runs.** All output is logged to `campaign/campaign.log` (not just stdout). A crash-resilient wrapper script auto-restarts the runner up to 3 times. PVC and local disk space are monitored continuously. Auth tokens are checked pre-flight. When the campaign finishes, a full summary is saved to disk with re-run commands for any failures.

## Problem

Running 40+ LLM benchmarking experiments manually is slow and error-prone. The current `/blis-inference-perf` skill runs one experiment at a time with Claude generating YAML — introducing hallucination risk, no retry logic, no multi-cluster support, and no batch orchestration.

## Solution

A **two-phase system**: a deterministic Python generator that pre-builds all pipeline artifacts, and a GPU-aware runner that executes them across clusters with monitoring, retry, download verification, and cleanup.

---

## Phase 1: Generator (`blis-campaign generate`)

### Purpose

Reads an experiment matrix (JSON) and produces a self-contained directory per experiment with all YAML files needed to run it. No AI involved — pure dict manipulation and tektonc compilation.

### Inputs

| File | Description |
|------|-------------|
| `experiments.json` | Flat array mirroring the [#598 table](https://github.com/inference-sim/inference-sim/discussions/598) (53 experiments, rows 1-53, phases 0-9). One entry per experiment. |
| `config/models.yaml` | Short model name → HuggingFace ID + optional extra vLLM args |
| `config/clusters.yaml` | HW type → kubectl context, GPU label, GPU capacity |
| `workloads.yaml` | Workload profiles (existing file in repo root) |
| `tektoncsample/blis-inference-perf/values.yaml` | Base values template (existing file) |
| `tektoncsample/blis-inference-perf/data_pipeline.yaml.j2` | Pipeline template (existing file) |

### Experiment JSON schema

Each entry mirrors the #598 table columns:

```json
{
  "id": 13,
  "model": "Llama-3.1-8b",
  "precision": "FP16",
  "hw": "H100",
  "workload": "general",
  "mbt": 2048,
  "cpu_offload": false,
  "gpu_mem": 0.9,
  "tp": 1,
  "dp": null,
  "notes": ""
}
```

Required fields: `id`, `model`, `precision`, `hw`, `workload`, `mbt`, `cpu_offload`, `gpu_mem`, `tp`.
Optional fields: `dp` (defaults to null), `notes`.

Valid `precision` values: `"FP16"` (default, no extra vLLM arg) and `"FP8"` (adds `--quantization fp8`).

**FP8 quantization approach** ([discussion](https://github.com/inference-sim/inference-sim/discussions/598#discussioncomment-16099342)): Online dynamic FP8 via `--quantization fp8`. Loads standard BF16 checkpoints from HuggingFace (same `modelArtifacts.uri` as FP16 experiments), quantizes Linear layer weights to FP8_E4M3 at initialization, computes activation scales dynamically per forward pass. No pre-quantized models or calibration data needed.

Hardware behavior:
- **H100**: Native FP8 tensor cores — full memory + throughput benefit.
- **A100**: No native FP8 compute — vLLM uses W8A16 via Marlin kernels (weights in FP8, upcasted to FP16 for matrix multiply). Memory savings preserved, reduced throughput gains.

### Model config (`config/models.yaml`)

Maps table model names to HuggingFace IDs. Most entries are a plain string. Models needing extra vLLM args use an object:

```yaml
Llama-3.1-8b: "meta-llama/Llama-3.1-8B-Instruct"
Qwen3-14B: "Qwen/Qwen3-14B"
Codellama-34b: "codellama/CodeLlama-34b-Instruct-hf"
Llama-2-70b: "meta-llama/Llama-2-70b-hf"
Mixtral-8x7B: "mistralai/Mixtral-8x7B-v0.1"
DeepSeek-V3: "deepseek-ai/DeepSeek-V3"
Llama-2-7b-hf: "meta-llama/Llama-2-7b-hf"

Llama-4-Scout-17B-16E:
  hf_id: "meta-llama/Llama-4-Scout-17B-16E"
  extra_vllm_args:
    - '--override-generation-config={"attn_temperature_tuning": true}'
```

**Note:** Model IDs match the HuggingFace links in the [#598 discussion](https://github.com/inference-sim/inference-sim/discussions/598). Mixtral uses the base model (`v0.1`), not Instruct. Llama-4-Scout uses the base, not Instruct.

Resolution logic:

```python
entry = models[experiment["model"]]
if isinstance(entry, str):
    hf_id, extra_args = entry, []
else:
    hf_id, extra_args = entry["hf_id"], entry.get("extra_vllm_args", [])
```

### Cluster config (`config/clusters.yaml`)

```yaml
namespace: "diya"  # all experiments run in this namespace

H100:
  context: "pokprod001"
  gpu_label_key: "nvidia.com/gpu.product"
  gpu_label_value: "NVIDIA-H100-80GB-HBM3"

A100-80GB:
  context: "fmaas-vllmd"
  gpu_label_key: "nvidia.com/gpu.product"
  gpu_label_value: "NVIDIA-A100-SXM4-80GB"

L40S:
  context: "fmaas-platform-eval"
  gpu_label_key: "nvidia.com/gpu.product"
  gpu_label_value: "NVIDIA-L40S"
```

No `gpu_capacity` field — the runner queries real-time GPU availability from the cluster before each scheduling decision (see GPU scheduling section).

### What changes per experiment (all three files)

**values.yaml** — the generator deep-copies the base template and overrides:

| Field | Source | Why |
|-------|--------|-----|
| `experiment.name` | `f"blis-{id}-{model}-{workload}" \| dns` | Pipeline resource name; must be unique for concurrent execution |
| `experiment.description` | Auto-generated from experiment params | Identification |
| `stack.MAX_NUM_BATCHED_TOKENS` | `mbt` from experiment JSON | Per-experiment config |
| `stack.treatments.tensorParallelism` | `[tp]` from experiment JSON | Loop domain |
| `stack.model.helmValues.decode.acceleratorTypes.labelValues` | `[gpu_label_value]` from clusters.yaml | GPU targeting |
| `stack.model.helmValues.decode.parallelism.tensor` | `tp` from experiment JSON | Tensor parallelism |
| `stack.model.helmValues.decode.parallelism.data` | `dp` from experiment JSON (if set) | Data parallelism |
| `stack.model.helmValues.decode.replicas` | `dp` from experiment JSON (if set) | Replica count |
| `stack.extra_overrides` | Built from experiment params (see below) | Extra vLLM args |
| `workload.profileTemplate.load` | `workloads[workload].load` | Workload profile |
| `workload.profileTemplate.data` | `workloads[workload].data` | Workload profile |

Constants (same across all experiments): `stack.MAX_MODEL_LEN` = 4096, `stack.MAX_NUM_SEQS` = 128.

Note: `modelArtifacts.uri`, `modelArtifacts.name`, and `fullnameOverride` are left as placeholders (`MODEL`, `FULLNAME`) in values.yaml. The `deploy-model` task's `apply-overrides` step automatically sets these from the `model` and `modelLabel` pipeline params at runtime.

**pipelinerun.yaml** — pre-generated as an inspectable template with a placeholder name:

| Field | Source | Why |
|-------|--------|-----|
| `metadata.name` | `__PIPELINE_RUN_NAME__` (placeholder) | Runner stamps actual name at deploy time: `blis-{id}-attempt{n}-{unix_ts}`. This handles retries (each attempt gets a unique name) and concurrency. |
| `spec.pipelineRef.name` | Extracted from compiled pipeline.yaml | Must match the pipeline resource |
| `params.experimentId` | `f"{id}-{model}-tp{tp}-{workload}"` | Determines PVC data path (`/mnt/exp/<experimentId>/`) |
| `params.model` | HF ID from models.yaml lookup | Used by download-model and deploy-model tasks |
| `params.namespace` | `"diya"` | Constant across all experiments |

The runner fills in the name at deploy time and records it in `campaign-state.json` for monitoring and cleanup:

```python
def deploy(exp_dir, context, attempt):
    name = f"blis-{exp['id']}-attempt{attempt}-{int(time.time())}"
    pr_yaml = read(exp_dir / "pipelinerun.yaml")
    pr_yaml = pr_yaml.replace("__PIPELINE_RUN_NAME__", name)
    kubectl_apply_stdin(pr_yaml, context)
    return name
```

**pipeline.yaml** — compiled by tektonc from the template + values.yaml. Changes implicitly because values.yaml changes (different TP, mbt, extra_overrides produce different compiled output). The pipeline name comes from `experiment.name | dns` in the template.

### Extra overrides builder

Constructs `stack.extra_overrides` list from experiment parameters. These become vLLM CLI args injected via the deploy-model task's override mechanism.

| Experiment field | Condition | vLLM arg generated |
|-----------------|-----------|-------------------|
| `precision` | `== "FP8"` | `--quantization fp8` |
| `gpu_mem` | `!= 0.9` | `--gpu-memory-utilization=<value>` |
| `cpu_offload` | `== true` | `--cpu-offload-gb <DEFAULT_CPU_OFFLOAD_GB>` |
| `dp` | `> 1` | `--data-parallel-size <dp>` |
| `dp` + MoE model | `dp > 1` and model is MoE | `--enable-expert-parallel` |
| model-specific | from `models.yaml` | e.g., `--override-generation-config=...` |

**Constants (configurable at top of generator):**

```python
DEFAULT_CPU_OFFLOAD_GB = 4  # GiB per GPU when cpu_offload=true

# Models that use expert parallelism when dp > 1
MOE_MODELS = {"Mixtral-8x7B", "DeepSeek-V3", "Llama-4-Scout-17B-16E"}
```

**Important vLLM flag corrections** (verified against v0.15.1 source):
- CPU offloading is `--cpu-offload-gb <N>` (float, per-GPU GiB), NOT `--enable-cpu-offload` (does not exist)
- Data parallelism requires `--data-parallel-size <N>` as a vLLM arg, in addition to Helm values for GPU allocation
- Expert parallelism (`--enable-expert-parallel`) is only for MoE models with dp > 1; it shards experts across TP*DP GPUs

### Pipeline compilation

After writing `values.yaml`, the generator runs:

```bash
python tektonc/tektonc.py \
  -t tektoncsample/blis-inference-perf/data_pipeline.yaml.j2 \
  -f <exp-dir>/values.yaml \
  -o <exp-dir>/pipeline.yaml
```

If tektonc fails, the generator **aborts immediately** with the error. This catches template issues before any GPU time is spent.

### PipelineRun generation

Generated from a template with unique naming:

```yaml
apiVersion: tekton.dev/v1
kind: PipelineRun
metadata:
  name: blis-<id>-<unix-timestamp>
spec:
  pipelineRef:
    name: <pipeline-name-from-pipeline.yaml>
  params:
    - name: experimentId
      value: "<experiment-id>"
    - name: model
      value: "<hf-model-id>"
    - name: namespace
      value: "diya"
  workspaces:
    - name: model-cache
      persistentVolumeClaim:
        claimName: model-pvc
    - name: hf-credentials
      secret:
        secretName: hf-secret
    - name: data
      persistentVolumeClaim:
        claimName: data-pvc
    - name: target-credentials
      secret:
        secretName: s3-secret
```

Unique names via timestamp suffix prevent collisions from retries.

### Validation at generation time

- JSON schema validation on each experiment entry
- Model name must exist in `models.yaml` (fail-fast on typos)
- HW must exist in `clusters.yaml`
- Workload must exist in `workloads.yaml`
- tektonc compilation must succeed

### Output structure

```
campaign/
  13-llama-3.1-8b-h100-general/
    values.yaml
    pipeline.yaml
    pipelinerun.yaml
    experiment.json
  25-llama-3.1-8b-a100-general/
    values.yaml
    pipeline.yaml
    pipelinerun.yaml
    experiment.json
  ...
```

All files can be inspected and diffed before execution.

---

## Phase 2: Runner (`blis-campaign run`)

### Purpose

Iterates over pre-generated experiment directories, deploys them to the correct cluster, monitors pipeline runs, downloads results, and cleans up. GPU-aware scheduling enables concurrent execution.

### GPU-aware scheduling

Each experiment needs `tp * max(dp, 1)` GPUs. The runner queries **real-time GPU availability** from each cluster and schedules using **order-preserving greedy backfill**:

1. Always try to start the **next experiment in table order** first
2. If it doesn't fit (not enough free GPUs), scan ahead for smaller experiments that do fit
3. When a running experiment completes, re-query availability and evaluate the pending queue

#### Real-time GPU query

Before each scheduling decision, the runner checks actual free GPUs:

```python
def get_available_gpus(context, gpu_label_key, gpu_label_value):
    """Query actual free GPUs on nodes matching the GPU label."""
    # Total allocatable GPUs on matching nodes
    nodes = kubectl_json(
        f"get nodes -l {gpu_label_key}={gpu_label_value}", context=context
    )
    total = sum(
        int(n["status"]["allocatable"].get("nvidia.com/gpu", 0))
        for n in nodes["items"]
    )

    # GPUs currently requested by running pods on those nodes
    pods = kubectl_json(
        "get pods --all-namespaces --field-selector=status.phase=Running",
        context=context
    )
    allocated = sum(
        int(c["resources"].get("requests", {}).get("nvidia.com/gpu", 0))
        for p in pods["items"]
        for c in p["spec"]["containers"]
    )

    return total - allocated
```

This means: if someone else takes GPUs mid-campaign, the runner sees it and waits. If GPUs free up from other users, the runner takes advantage.

#### Scheduler loop

```python
pending = deque(all_experiment_dirs)  # in table order
running = {}  # exp_id -> RunningExperiment

while pending or running:
    # Query real GPU availability per cluster
    available = {
        hw: get_available_gpus(cfg["context"], cfg["gpu_label_key"], cfg["gpu_label_value"])
        for hw, cfg in clusters.items()
    }

    # Start experiments that fit
    for exp_dir in list(pending):
        exp = read_json(exp_dir / "experiment.json")
        gpus_needed = exp["tp"] * max(exp.get("dp") or 1, 1)

        if available[exp["hw"]] >= gpus_needed:
            pr = deploy(exp_dir, clusters[exp["hw"]]["context"])
            running[exp_dir] = RunningExperiment(pr, gpus_needed, exp["hw"])
            available[exp["hw"]] -= gpus_needed
            pending.remove(exp_dir)

    # Poll all running experiments
    sleep(30)
    for exp_dir, run in list(running.items()):
        status = check_status(run.pipeline_run, context=clusters[run.hw]["context"])
        if status in ("Succeeded", "Failed", "timeout"):
            handle_completion(exp_dir, run, status, pending)
            del running[exp_dir]
```

### Concurrent cluster access

Every kubectl/tkn/helm command uses `--context=<name>` explicitly instead of switching the global kubeconfig context. This is safe for concurrent operations across clusters.

### Campaign state (`campaign-state.json`)

Persisted after every state transition. Enables resume after crash/kill.

```json
{
  "experiments": {
    "13-llama-3.1-8b-h100-general": {
      "status": "completed",
      "attempts": 1,
      "started_at": "2026-03-11T10:00:00Z",
      "completed_at": "2026-03-11T11:30:00Z",
      "pipeline_run": "blis-13-1741686000"
    },
    "25-llama-3.1-8b-a100-general": {
      "status": "running",
      "attempts": 1,
      "started_at": "2026-03-11T11:31:00Z",
      "pipeline_run": "blis-25-1741691460"
    }
  }
}
```

**Resume behavior:** On restart, experiments in `deploying`/`running` state are treated as failed attempts (pipeline state is unknown). The runner cleans up any orphaned resources and retries.

### Experiment lifecycle

```
pending → deploying → running → downloading → completed
               ↓          ↓          ↓
           retrying    retrying   download_failed
               ↓          ↓
            failed      failed
```

### Post-experiment sequence

1. Pipeline run succeeds (detected via `tkn pr describe --context=X -o json`)
2. Download results from PVC to `results/<exp-id>/` locally (with verification)
3. Delete the PipelineRun resource (`kubectl delete pipelinerun <name> --context=X`)
4. Verify no orphaned helm releases (`helm list --context=X`; warn if found)
5. Free GPUs in scheduler pool
6. Update campaign state to `completed`

Data stays on PVC (S3 upload happens within the pipeline). PipelineRun gets cleaned up.

### Retry policy

**Retry once, then skip.** On failure:
1. Run failure diagnostics (see below)
2. Clean up the failed PipelineRun
3. If `attempts < 2`: re-enqueue to pending, increment attempt count
4. If `attempts >= 2`: mark as `failed`, log the error, move on

**Special case — model-pvc full:** If diagnostics identify "No space left on device" from the `download-model` task, run reactive model cleanup (see below) before retrying. This doesn't count as an extra attempt.

### Model PVC reactive cleanup

When an experiment fails because `model-pvc` is full (the `download-model` task can't write weights), the runner frees space by deleting model weights that aren't in use by currently running experiments on that cluster. The failed experiment then retries, and `download-model` re-downloads the model it needs into the freed space.

All operations target the `diya` namespace via a busybox pod with `model-pvc` mounted.

```python
def handle_model_pvc_full(context, namespace, running_exps_on_cluster):
    """Reactive cleanup: delete model weights not used by running experiments."""
    # Models currently in use — DO NOT delete
    protected = {
        resolve_model(e["model"])  # e.g. "meta-llama/Llama-2-70b-hf"
        for e in running_exps_on_cluster
    }

    # List all model dirs on the PVC
    all_models = kubectl_exec_busybox(
        "ls /models/", context=context, namespace=namespace, pvc="model-pvc"
    ).split()

    freed_gb = 0
    for model_dir in all_models:
        if model_dir not in protected:
            size = kubectl_exec_busybox(
                f"du -s /models/{model_dir} | cut -f1",
                context=context, namespace=namespace, pvc="model-pvc"
            )
            log.info(f"Evicting {model_dir} ({int(size)//1048576} GB) from model-pvc")
            kubectl_exec_busybox(
                f"rm -rf /models/{model_dir}",
                context=context, namespace=namespace, pvc="model-pvc"
            )
            freed_gb += int(size) // 1048576

    log.info(f"Freed ~{freed_gb} GB from model-pvc on {context}")
    return freed_gb
```

**Safety guarantees:**
- Only deletes models **not used by currently running experiments** on that cluster. Pending experiments are not protected — they'll re-download when they start.
- `download-model` is idempotent: if weights exist on the PVC it skips download; if they were deleted it re-downloads from HuggingFace. No corruption risk.
- Per-cluster isolation: cleaning model-pvc on pokprod001 doesn't affect model-pvc on fmaas-vllmd.

**Example output:**
```
FAILED #17 DeepSeek-V3 H100 general: download-model — "No space left on device" (attempt 1/2)
  → model-pvc cleanup: deleting codellama/CodeLlama-34b-Instruct-hf (68 GB),
    meta-llama/Llama-2-70b-hf (138 GB)
  → Freed ~206 GB. Retrying #17...
```

**Cost:** Re-downloading an evicted model takes ~10-30 min depending on size and network. This only triggers when the PVC is actually full, which may never happen with a sufficiently large PVC (see sizing recommendations in the campaign config).

### Monitoring

Polls `tkn pr describe <name> --context=X -o json` every 30 seconds. Extracts:
- Overall status: `Succeeded` / `Failed` / `Running`
- Current task name (for progress logging)
- Failure reason (for error reporting)

**Stall detection:** Tracks the last task state transition time. If no progress for 60 minutes, treats it as a timeout failure.

### Failure diagnostics

On **any** failure (pipeline failure, timeout, stall), before cleanup, the runner collects diagnostic data and runs basic triage. This is critical for new configurations (FP8, EP, DP, new models) that are likely to fail in novel ways.

#### Data collection (all kubectl, no AI)

```python
def collect_diagnostics(exp_dir, exp, pipeline_run, context, namespace):
    diag_dir = exp_dir / "diagnosis"
    diag_dir.mkdir(exist_ok=True)

    # 1. Pipeline run status + per-task breakdown
    save(diag_dir / "pipeline-status.json",
         run_cmd(f"tkn pr describe {pipeline_run} --context={context} -o json"))

    # 2. Pod statuses (Pending? CrashLoopBackOff? OOMKilled?)
    model_label = get_model_label(exp)
    save(diag_dir / "pods.json",
         run_cmd(f"kubectl get pods -n {namespace} -l app={model_label} "
                 f"--context={context} -o json"))

    # 3. Namespace events (scheduling failures, image pull errors)
    save(diag_dir / "events.txt",
         run_cmd(f"kubectl get events -n {namespace} --context={context} "
                 f"--sort-by=.lastTimestamp"))

    # 4. vLLM container logs (if pod started at all)
    for pod in get_model_pods(namespace, model_label, context):
        save(diag_dir / f"vllm-logs-{pod}.txt",
             run_cmd(f"kubectl logs {pod} -n {namespace} -c vllm "
                     f"--context={context} --tail=500",
                     ignore_errors=True))

    # 5. Helm release status
    save(diag_dir / "helm-status.txt",
         run_cmd(f"helm status {model_label} --context={context}",
                 ignore_errors=True))

    # 6. Node GPU state
    gpu_label = clusters[exp["hw"]]
    save(diag_dir / "gpu-nodes.txt",
         run_cmd(f"kubectl get nodes -l {gpu_label['gpu_label_key']}="
                 f"{gpu_label['gpu_label_value']} --context={context} "
                 f"-o custom-columns=NAME:.metadata.name,"
                 f"GPUs:.status.allocatable.nvidia\\.com/gpu,"
                 f"STATUS:.status.conditions[-1].type"))

    # 7. Triage
    summary = run_triage(diag_dir)
    save(diag_dir / "summary.txt", summary)
    return summary
```

#### Automated triage (pattern matching)

The runner scans collected data for known failure patterns:

```python
TRIAGE_PATTERNS = [
    # Pod-level issues
    ("pods.json",    pod_phase == "Pending",
     "Model pod stuck in Pending — likely insufficient GPUs or resource quota"),
    ("pods.json",    restart_count > 0,
     "Model pod in CrashLoopBackOff — check vllm-logs-*.txt for startup error"),
    ("pods.json",    terminated_reason == "OOMKilled",
     "OOMKilled — model too large for this GPU/TP config"),

    # vLLM startup errors
    ("vllm-logs-*",  "CUDA out of memory",
     "CUDA OOM during model loading — try higher TP or enable cpu_offload"),
    ("vllm-logs-*",  "does not support FP8",
     "Model or hardware does not support FP8 quantization"),
    ("vllm-logs-*",  "trust_remote_code",
     "Model requires --trust-remote-code flag"),
    ("vllm-logs-*",  "torch.cuda.OutOfMemoryError",
     "GPU OOM — insufficient GPU memory for model + KV cache"),
    ("vllm-logs-*",  "expert_parallel",
     "Expert parallelism configuration error — check TP/DP/EP compatibility"),

    # Kubernetes scheduling
    ("events.txt",   "FailedScheduling",
     "Kubernetes couldn't schedule pod — check GPU availability and node affinity"),
    ("events.txt",   "ImagePullBackOff",
     "Failed to pull vLLM container image"),
    ("events.txt",   "Insufficient nvidia.com/gpu",
     "Not enough GPUs available on matching nodes"),

    # Pipeline-level
    ("pipeline-status.json", any_task_failed,
     "Task '{task_name}' failed — see task-level logs in pipeline-status.json"),
]
```

#### Output

For each failed experiment, the runner:
1. Saves raw diagnostics to `campaign/<exp-id>/diagnosis/`
2. Prints a triage summary to stdout:

```
=== DIAGNOSIS: #15 DeepSeek-V3 H100 General (attempt 1/2) ===
Failure type: timeout (no progress for 60 min)
Failed at task: deploy-model-8

Triage:
  - Model pod stuck in Pending — likely insufficient GPUs or resource quota
  - FailedScheduling: "0/4 nodes are available: insufficient nvidia.com/gpu"

Raw diagnostics: campaign/15-deepseek-v3-h100-general/diagnosis/
Retrying...
```

3. Records the triage summary in `campaign-state.json`:

```json
{
  "15-deepseek-v3-h100-general": {
    "status": "failed",
    "attempts": 2,
    "last_failure": "Model pod stuck in Pending — insufficient GPUs",
    "diagnosis_path": "campaign/15-deepseek-v3-h100-general/diagnosis/"
  }
}
```

After the campaign completes, all failures are listed in the final summary with their triage, so you have a starting point to fix and re-run with `--only`.

---

## Download & Verification

The download step is a deterministic script with full verification. No silent partial downloads.

### Expected file manifest

```python
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

REQUIRED_PATTERNS = [
    "results/stage_*_lifecycle_metrics.json",  # at least one stage file
]
```

### Download flow

```python
def download_and_verify(exp_dir, exp_id, context, namespace, max_retries=2):
    local_dest = results_dir / exp_id

    for attempt in range(max_retries):
        pod_name = None
        try:
            # 1. Check local disk space
            if get_free_disk_gb(local_dest.parent) < 1.0:
                raise DownloadError("Insufficient local disk space")

            # 2. Create busybox pod as root (avoids UID mismatch with vLLM)
            pod_name = create_busybox_pod(context, namespace, run_as_root=True)
            wait_for_pod_ready(pod_name, context, timeout=120)

            # 3. Verify remote data exists
            remote_files = kubectl_exec(
                pod_name, f"find /mnt/exp/{exp_id} -type f",
                context=context, timeout=30
            )
            if not remote_files:
                raise DownloadError(f"No files at /mnt/exp/{exp_id}")

            # 4. Copy via tar pipe (avoids kubectl cp tar warning corruption bug)
            if local_dest.exists():
                shutil.rmtree(local_dest)
            tar_copy(pod_name, f"/mnt/exp/{exp_id}", local_dest,
                     context=context, timeout=600)

            # 5. Verify file count matches
            local_count = count_files(local_dest)
            if local_count != len(remote_files):
                raise DownloadError(
                    f"File count mismatch: {local_count} local vs {len(remote_files)} remote"
                )

            # 6. Verify required files exist and are non-empty
            missing, empty = [], []
            for f in REQUIRED_FILES:
                path = local_dest / f
                if not path.exists():
                    missing.append(f)
                elif path.stat().st_size == 0:
                    empty.append(f)
            for pattern in REQUIRED_PATTERNS:
                if not list(local_dest.glob(pattern)):
                    missing.append(pattern)

            if missing or empty:
                raise DownloadError(f"Missing: {missing}, Empty: {empty}")

            return DownloadResult(success=True, file_count=local_count)

        except DownloadError as e:
            log(f"Download attempt {attempt+1} failed: {e}")
            if local_dest.exists():
                shutil.rmtree(local_dest)
            if attempt >= max_retries - 1:
                raise
        finally:
            if pod_name:
                force_delete_pod(pod_name, context)
```

### Download failure handling

If verification fails after all retries:
- Mark experiment as `download_failed` (distinct from pipeline failure)
- **Do NOT delete the PipelineRun** — preserve state for manual investigation
- Log exactly which files are missing/empty
- Move to next experiment

---

## Error Handling

### Pre-flight checks (run at startup, fail fast)

| Check | Action on failure |
|-------|-------------------|
| `kubectl`/`tkn`/`helm` installed | Abort with "install X" message |
| Cluster contexts reachable | `kubectl --context=X cluster-info` per cluster; abort if unreachable |
| Namespace exists | `kubectl get namespace` per cluster; abort |
| Pipeline tasks deployed | `kubectl get task` for required tasks; abort with list of missing tasks |
| GPUs queryable per cluster | `get_available_gpus()` must return ≥ 0; abort if nodes not found for a referenced HW label |
| Auth valid | `kubectl auth can-i create pipelinerun` per cluster; abort |

### Runtime errors

| Error | Mitigation |
|-------|-----------|
| PipelineRun name collision | Unique names via `blis-<id>-<unix-timestamp>` |
| `kubectl apply` fails | Capture stderr, mark `failed`, log error, move on |
| Orphaned helm release from previous run | Pre-deploy `helm list` check; warn + clean up before deploying |
| `tkn pr describe` transient failure | Retry 3x with 10s backoff before treating as error |
| Auth token expires mid-campaign | Pre-flight: verify token TTL or run `oc whoami` per cluster; warn if token expires within 24h. Runtime: detect 401/403, retry auth check 3x with 30s backoff, then pause with clear message and persist state for resume. See Unattended Operation section. |
| Pipeline stuck (no progress for 60 min) | Treat as timeout failure |
| Busybox pod won't start | Retry pod creation once with 60s wait; then `download_failed` |
| `kubectl cp` corruption | Use `tar cf - \| tar xf -` pipe instead of `kubectl cp` |
| Large file timeout | 10 min timeout on tar copy; retry once |
| Local disk full | Check free space before download; if below 2 GB, **halt campaign** (not just skip) — all subsequent downloads would also fail. Log "disk full, pausing" and persist state. |
| **data-pvc** filling up | After each experiment completes, check data-pvc free space via busybox `df`. Warn at 80% full. At 95% full, halt new experiment launches (data corruption risk). Log remaining space per experiment. |
| **model-pvc** filling up | Detected when `download-model` task fails with "No space left on device". Reactive cleanup: delete model weights not used by currently running experiments, then retry. See Model PVC Reactive Cleanup section. |
| PipelineRun delete fails | Log warning, continue (not blocking) |
| Orphaned helm release detected post-run | Log warning with exact `helm delete` command; don't auto-delete |

---

## Unattended Operation

The campaign runner is designed to run overnight without human intervention. Six concerns are addressed:

### 1. File logging

All output goes to **both** stdout and a log file (`campaign/campaign.log`). The log file is append-only and includes timestamps, so terminal history overflow doesn't lose information.

```python
import logging, sys

def setup_logging(campaign_dir):
    log_file = campaign_dir / "campaign.log"
    fmt = "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, mode="a"),
        ],
    )
```

Each experiment's diagnostics are also saved to `campaign/<exp-id>/diagnosis/` (already covered above), so even if the terminal is gone, the full record exists on disk.

### 2. Auth token pre-flight

Before launching the campaign, the runner checks token health per cluster:

```python
def check_auth_health(context):
    """Warn if auth token will expire before campaign likely finishes."""
    # Try oc whoami --show-token to check expiry (OpenShift)
    result = run_cmd(f"oc whoami --show-token --context={context}", ignore_errors=True)
    if result.returncode != 0:
        # Non-OCP cluster, or token not available — check basic auth
        result = run_cmd(f"kubectl auth can-i create pipelinerun --context={context}")
        if result.returncode != 0:
            raise AuthError(f"Cannot authenticate to {context}")
        log.warning(f"Cannot determine token TTL for {context}; ensure long-lived token")
        return

    # If we can decode the token, check expiry
    # ... token TTL check logic ...
```

If the token looks short-lived (< 12 hours), the runner prints a warning:
```
WARNING: Token for pokprod001 may expire in ~8h. Campaign has ~53 experiments.
Consider: oc login --token=<long-lived-token> or use a service account.
```

### 3. PVC space monitoring

After each experiment downloads results, the runner checks PVC free space:

```python
def check_pvc_space(context, namespace, threshold_pct=80, critical_pct=95):
    """Check PVC usage via busybox pod."""
    output = kubectl_exec_busybox(
        "df -h /mnt/exp | tail -1", context=context, namespace=namespace
    )
    usage_pct = parse_df_usage(output)
    if usage_pct >= critical_pct:
        raise PVCFullError(f"PVC {usage_pct}% full — halting to prevent data corruption")
    if usage_pct >= threshold_pct:
        log.warning(f"PVC {usage_pct}% full — consider cleaning old experiment data")
```

Thresholds: **warn at 80%**, **halt at 95%**.

### 4. Local disk space early halt

Unlike the per-download check (which skips one experiment), this is a campaign-level guard. If local disk drops below 2 GB, the runner **halts the entire campaign** rather than skipping individual downloads — because all subsequent downloads would also fail.

### 5. Crash recovery wrapper

The runner persists state after every transition (`campaign-state.json`), so it can always resume. But if the Python process itself crashes (segfault, OOM, power loss), nobody restarts it. The recommended approach is a simple wrapper script:

```bash
#!/bin/bash
# run-campaign.sh — crash-resilient wrapper
# Usage: ./run-campaign.sh [args passed to blis-campaign run]
MAX_RESTARTS=3
RESTART_COUNT=0

while [ $RESTART_COUNT -lt $MAX_RESTARTS ]; do
    echo "[$(date)] Starting campaign runner (attempt $((RESTART_COUNT+1))/$MAX_RESTARTS)"
    python -m blis_campaign run "$@"
    EXIT_CODE=$?

    if [ $EXIT_CODE -eq 0 ]; then
        echo "[$(date)] Campaign completed successfully."
        break
    fi

    RESTART_COUNT=$((RESTART_COUNT+1))
    echo "[$(date)] Runner exited with code $EXIT_CODE. Restart $RESTART_COUNT/$MAX_RESTARTS in 30s..."
    sleep 30
done

if [ $RESTART_COUNT -ge $MAX_RESTARTS ]; then
    echo "[$(date)] FATAL: Runner crashed $MAX_RESTARTS times. Check campaign/campaign.log"
fi
```

The runner uses exit code conventions:
- **0**: Campaign completed (all experiments finished or skipped)
- **1**: Unrecoverable error (bad config, all clusters unreachable)
- **2**: Crash/unexpected error (wrapper should restart)

### 6. Campaign completion summary

When the campaign finishes (all experiments completed, failed, or skipped), the runner prints and logs a final summary:

```
═══════════════════════════════════════════════════════
CAMPAIGN COMPLETE — 2026-03-12T08:30:00Z
═══════════════════════════════════════════════════════
Total:     53 experiments
Completed: 41
Failed:     8  (see details below)
Skipped:    4  (deprioritized)

FAILED EXPERIMENTS:
  #15 DeepSeek-V3 H100 general    — OOMKilled (2 attempts)
  #17 DeepSeek-V3 FP8 codegen     — deploy-model timeout (2 attempts)
  #36 Llama-4-Scout EP sweep       — expert_parallel config error
  ...

Re-run failed experiments with:
  python -m blis_campaign run --campaign campaign/ --only 15,17,36,...

Full log: campaign/campaign.log
═══════════════════════════════════════════════════════
```

This summary is also saved to `campaign/campaign-summary.txt` so it's available even if the terminal is closed.

---

## Template Changes

Two small changes to existing files:

### `tektoncsample/blis-inference-perf/data_pipeline.yaml.j2`

Add after the existing override lines (after line ~115):

```yaml
{% for override in stack.extra_overrides | default([]) %}
                - {{ override }}
{% endfor %}
```

### `tektoncsample/blis-inference-perf/values.yaml`

Add under `stack:`:

```yaml
  extra_overrides: []
```

---

## Claude Skill Wrapper

Thin Claude skill (`/blis-campaign`) that wraps the Python CLI:

- **`/blis-campaign generate`** — Claude runs `python -m blis_campaign generate ...` directly. Fast (seconds). Shows summary of generated files.
- **`/blis-campaign status`** — Claude runs `python -m blis_campaign status ...` directly. Fast. Shows progress table.
- **`/blis-campaign run [--range FROM-TO] [--only ID,ID,...]`** — The runner is long-lived (hours/days), so Claude **does not run it**. Instead, Claude prints the exact command for you to run in a `tmux`/`screen` session:
  ```
  Run this in a tmux session:
    ./blis-campaign/run-campaign.sh --campaign campaign/ --hw H100 --range 13-35
  ```
  The wrapper script auto-restarts the runner on unexpected crashes (up to 3 times). All output is logged to `campaign/campaign.log`.

The skill does no YAML generation, monitoring, or pipeline logic. It's a convenience layer for generate/status and a command builder for run. You can also skip the skill entirely and use the Python CLI directly.

---

## Module Layout

### CLI interface

```
blis-campaign generate --experiments experiments.json --output campaign/
blis-campaign run --campaign campaign/ [--hw H100] [--range 13-35] [--only 13,25,39]
blis-campaign status --campaign campaign/
```

- `--hw H100` or `--hw H100,A100-80GB` — Only run experiments targeting these hardware types. Matches the `hw` field in experiment JSON. Use this when you only have access to some clusters.
- `--range FROM-TO` — Run experiments with IDs from FROM to TO inclusive. E.g., `--range 13-35` runs experiments 13, 14, 15, ..., 35.
- `--only ID,ID,...` — Run only these specific experiment IDs. E.g., `--only 13,25,39`.
- No flags — runs all experiments on all clusters.

All filters are AND'd together. `--hw H100 --range 13-35` runs only H100 experiments with IDs 13-35. Pre-flight checks only validate clusters that have pending experiments after filtering.

---

## Module Layout

```
blis-campaign/
  DESIGN.md                # This file
  __main__.py              # CLI: generate / run / status
  generate.py              # Phase 1: experiments.json → per-experiment directories
  run.py                   # Phase 2: GPU-aware scheduler + deploy/monitor loop
  state.py                 # Campaign state persistence
  cluster.py               # Cluster context management + pre-flight checks
  download.py              # PVC download with tar pipe + verification
  cleanup.py               # PipelineRun deletion, orphan detection
  run-campaign.sh          # Crash-resilient wrapper script (restarts on unexpected exit)
  config/
    models.yaml            # Short name → HF ID mapping
    clusters.yaml          # HW → context, GPU label, capacity
```

Existing files modified:
- `tektoncsample/blis-inference-perf/data_pipeline.yaml.j2` — add `extra_overrides` loop (3 lines)
- `tektoncsample/blis-inference-perf/values.yaml` — add `stack.extra_overrides: []`
