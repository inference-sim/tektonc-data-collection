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
- Runs all matching experiments with GPU-aware parallel scheduling (default: up to 16 GPUs)
- Retries each experiment once on failure, with full diagnostics
- Restarts the runner up to 3 times if it crashes (network blip, kubectl timeout, etc.)
- Picks up where it left off on restart — state is persisted to `campaign-state.json`
- Logs everything to `campaign/campaign.log`

Detach from tmux with `Ctrl-b d` and come back later with `tmux attach -t blis`.

To limit GPU usage or run a subset:

```bash
# Use at most 8 GPUs
./blis-campaign/run-campaign.sh --campaign blis-campaign/campaign/ --hw H100 --max-gpus 8

# Run only experiments 13 through 24
./blis-campaign/run-campaign.sh --campaign blis-campaign/campaign/ --hw H100 --range 13-24

# Run specific experiments by ID
./blis-campaign/run-campaign.sh --campaign blis-campaign/campaign/ --hw H100 --only 16,17,18
```

### 3. Monitor progress

From another terminal:

```bash
# Live log
tail -f blis-campaign/campaign/campaign.log

# Status summary
python blis-campaign status --campaign blis-campaign/campaign/
```

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
  "notes": ""
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
| `notes` | Free-text notes (e.g., "completed", "mbt sweep") |

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

**Stalled experiment**: If no progress is detected for 60 minutes, the runner times out the experiment, collects diagnostics, and retries once.
