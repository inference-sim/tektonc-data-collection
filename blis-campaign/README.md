# BLIS Campaign Runner

Batch runner for BLIS LLM benchmarking experiments. Takes a table of experiments (model + hardware + workload combinations), generates Tekton pipeline YAML for each one, then runs them on GPU clusters with automatic scheduling, retries, and result download.

## Quick Start

### 1. Generate experiment pipelines

```bash
python blis-campaign generate \
  --experiments blis-campaign/experiments.json \
  --output blis-campaign/campaign/
```

This reads `experiments.json` (the experiment table) and produces one directory per experiment under `campaign/`, each containing:

```
campaign/
  13-qwen3-14b-h100-general/
    experiment.json      # experiment config (copy from table)
    values.yaml          # computed Helm values for this experiment
    pipeline.yaml        # compiled Tekton Pipeline
    pipelinerun.yaml     # PipelineRun template (name stamped at deploy time)
```

### 2. Launch the campaign (unattended)

Start a tmux or screen session and run the crash-resilient wrapper:

```bash
tmux new -s blis
./blis-campaign/run-campaign.sh --campaign blis-campaign/campaign/ --hw H100
```

This is the recommended way to run. The wrapper:
- Runs all matching experiments with GPU-aware parallel scheduling (default: up to 16 GPUs, 4 concurrent PipelineRuns)
- Retries each experiment once on failure, with full diagnostics
- Restarts the runner up to 3 times if it crashes (network blip, kubectl timeout, etc.)
- Picks up where it left off on restart — state is persisted to `campaign-state.json`
- Logs everything to `campaign/campaign.log`

Detach from tmux with `Ctrl-b d` and come back later with `tmux attach -t blis`.

To limit GPU usage or run a subset:

```bash
# Use at most 8 GPUs
./blis-campaign/run-campaign.sh --campaign blis-campaign/campaign/ --hw H100 --max-gpus 8

# Limit to 2 concurrent PipelineRuns (default: 4)
./blis-campaign/run-campaign.sh --campaign blis-campaign/campaign/ --hw H100 --max-concurrent 2

# Run only experiments 13 through 24
./blis-campaign/run-campaign.sh --campaign blis-campaign/campaign/ --hw H100 --range 13-24

# Run specific experiments by ID
./blis-campaign/run-campaign.sh --campaign blis-campaign/campaign/ --hw H100 --only 16,17,18

# Include unsafe/blocked/uncalibrated experiments (default: safe only)
./blis-campaign/run-campaign.sh --campaign blis-campaign/campaign/ --hw H100 --all
```

By default, only experiments marked `"safe": "safe"` in `experiment.json` are run. Use `--all` to override this and include unsafe, blocked, or uncalibrated experiments.

### 3. Monitor progress

All monitoring commands run from a **separate terminal** while the campaign is active.

#### Status snapshot

```bash
python blis-campaign status --campaign blis-campaign/campaign/
```

Shows a summary of the campaign state:
- Aggregate counts by status (completed, running, deploying, pending, failed, skipped)
- Progress fraction (e.g. `Progress: 12/53 completed`)
- Active experiments with their PipelineRun names and start times
- Failed experiments with failure reasons and attempt counts

This reads `campaign-state.json` atomically, so it's safe to run concurrently with the runner.

#### Live log

```bash
tail -f blis-campaign/campaign/campaign.log
```

The runner logs to both stdout and `campaign.log`. Key events to look for:

| Log message | Meaning |
|-------------|---------|
| `STARTED #16 Llama-3.1-8b (1 GPUs) -> blis-16-attempt1-...` | Experiment deployed |
| `16-llama-3-1-8b-h100-general: deploy-model (Running)` | Task progress update |
| `SUCCEEDED 16-llama-3-1-8b-h100-general` | Pipeline finished, downloading results |
| `COMPLETED 16-llama-3-1-8b-h100-general` | Results downloaded successfully |
| `FAILED #17 DeepSeek-V3 H100 general: OOM (attempt 1/2)` | Experiment failed (will retry) |
| `TIMEOUT ...: no progress for 180 min` | Stall detected, auto-failing |

#### Deep-dive with Tekton CLI

PipelineRun names follow the pattern `blis-{id}-attempt{N}-{timestamp}`. Get the name from the status command or log, then:

```bash
# List all pipeline runs
tkn pr list -n diya

# Watch live logs of a running pipeline
tkn pr logs blis-16-attempt1-1718000000 -n diya -f

# Full task breakdown and status
tkn pr describe blis-16-attempt1-1718000000 -n diya
```

#### Raw state file

```bash
python -m json.tool blis-campaign/campaign/campaign-state.json
```

The state file tracks every experiment's status, current attempt, PipelineRun name, and failure history. It persists across runner restarts.

## Running Directly (without wrapper)

If you prefer running without the crash-resilient wrapper:

```bash
python blis-campaign run \
  --campaign blis-campaign/campaign/ \
  --hw H100
```

This does the same scheduling/retry logic but won't auto-restart if the Python process itself crashes.

## Running with Claude

If you use Claude Code, you can ask it to run experiments interactively:

```
> Run BLIS experiments 13-24 on H100
```

Claude will use the `/blis-inference-perf` skill to deploy and monitor individual experiments. The campaign runner is for unattended batch execution when you want to run many experiments overnight.

## Clusters

Three GPU clusters are configured in `config/clusters.yaml`:

| Hardware | Cluster Context | GPUs |
|----------|----------------|------|
| H100 | pokprod001 | NVIDIA H100 80GB HBM3 |
| A100-80GB | fmaas-vllmd | NVIDIA A100 SXM4 80GB |
| L40S | fmaas-platform-eval | NVIDIA L40S |

The `--hw` flag selects which cluster to target. Only one cluster runs at a time.

## Experiment Table Format

`experiments.json` is a JSON array where each entry defines one experiment:

```json
{
  "id": 17,
  "model": "DeepSeek-V3",
  "precision": "FP8",
  "hw": "H100",
  "workload": "general",
  "mbt": 2048,
  "cpu_offload": false,
  "gpu_mem": 0.9,
  "tp": 8,
  "dp": 1,
  "safe": "blocked",
  "done": false,
  "notes": "FP8 MoE OOM — blocked"
}
```

| Field | Description |
|-------|-------------|
| `id` | Unique experiment number |
| `model` | Model short name (mapped to HuggingFace ID in `config/models.yaml`) |
| `precision` | FP16 or FP8 |
| `hw` | Target hardware: H100, A100-80GB, or L40S |
| `workload` | Workload profile: general, codegen, roleplay, reasoning |
| `mbt` | MAX_NUM_BATCHED_TOKENS |
| `cpu_offload` | Enable CPU offloading (4GB) |
| `gpu_mem` | GPU memory utilization (0.0-1.0) |
| `tp` | Tensor parallelism degree |
| `dp` | Data parallelism degree (null = 1) |
| `safe` | Calibration status: `safe`, `unsafe`, `blocked`, or `uncalibrated` |
| `done` | Whether the experiment has been completed (`true`/`false`) |
| `notes` | Free-text notes (e.g., "completed", "mbt sweep") |

The `safe` field controls runtime filtering: by default, `run` only executes experiments where `safe` is `"safe"`. The `done` field controls generation: `generate` skips experiments where `done` is `true`.

## File Structure

```
blis-campaign/
  __main__.py          # CLI entry point (generate / run / status)
  generate.py          # Phase 1: builds per-experiment YAML from experiments.json
  run.py               # Phase 2: GPU-aware scheduler, deploy, monitor, retry
  state.py             # Campaign state persistence (campaign-state.json)
  cluster.py           # kubectl/helm wrappers, GPU queries, pre-flight checks
  download.py          # PVC data download via tar pipe + file verification
  cleanup.py           # PipelineRun cleanup, failure diagnostics, triage
  run-campaign.sh      # Crash-resilient bash wrapper (restarts up to 3x)
  experiments.json     # Full experiment table (53 experiments)
  config/
    models.yaml        # Model short name -> HuggingFace ID mapping
    clusters.yaml      # Cluster contexts and GPU labels
  campaign/            # Generated output (one directory per experiment)
```

## Troubleshooting

**Pre-flight check fails**: The runner validates cluster connectivity, namespace, Tekton tasks, GPU availability, and auth before starting. Fix the reported issue and re-run.

**Experiment fails**: Diagnostics are saved to `campaign/<experiment>/diagnosis/` with:
- `pipeline-status.json` - Full PipelineRun status
- `events.txt` - Kubernetes events
- `pods.txt` - Pod status
- `triage.txt` - Automated pattern matching against common failures (OOM, image pull, timeout, etc.)

**Re-running failed experiments**: Use `--only` with the failed experiment IDs:
```bash
python blis-campaign run --campaign blis-campaign/campaign/ --hw H100 --only 17,18
```

**Retrying failed downloads**: If experiments are stuck in `download_failed` (the PVC data is still intact), retry just the download step:
```bash
python blis-campaign retry-downloads --campaign blis-campaign/campaign/ --hw H100

# Retry specific experiments only
python blis-campaign retry-downloads --campaign blis-campaign/campaign/ --hw H100 --only 17,18
```

**Recovering orphaned experiments**: If the runner was interrupted while experiments were running, use `harvest` to recover their data:
```bash
python blis-campaign harvest --campaign blis-campaign/campaign/ --hw H100

# Wait for still-running experiments to finish
python blis-campaign harvest --campaign blis-campaign/campaign/ --hw H100 --wait
```

The runner also automatically recovers orphans on restart — any experiments stuck in `running`, `deploying`, or `downloading` status will be resolved before new experiments launch.

**Graceful shutdown**: Press Ctrl-C once to stop launching new experiments while letting running ones finish. Press Ctrl-C again to force exit immediately.

**Stalled experiment**: If no new Tekton task starts for 3 hours (`STALL_TIMEOUT` in `run.py`), the runner times out the experiment, collects diagnostics, and retries once. The timer resets each time the pipeline advances to a new task, so long pipelines are fine — only genuinely stuck experiments get timed out.
