# BLIS Observe-Replay-Calibrate (ORC) Pipeline

This pipeline implements the observe-replay-calibrate loop for LLM benchmarking with simulator validation.

## Overview

The ORC workflow has three phases:

1. **Observe**: Send real requests to a live vLLM server, record TraceV2 (per-request timestamps, TTFT, E2E latency, token counts)
2. **Replay**: Feed the trace into the BLIS discrete-event simulator offline, producing simulated latencies
3. **Calibrate**: Compare real vs simulated latencies, output calibration report with MAPE, PearsonR, quality ratings

## Key Advantages

- **GPU efficiency**: Model deleted immediately after observe phase; replay and calibrate are offline (CPU-only)
- **Validation metrics**: Produces accuracy metrics for simulator quality (MAPE, Pearson correlation)
- **Scientific value**: Validates how well BLIS models real inference behavior

## Pipeline Structure

```
download-model → install-blis → [per-stack loop]:
  create-exp-config → deploy-model → observe → delete-model → replay → calibrate → upload
```

**Critical GPU optimization**: The model is deleted immediately after the observe phase, releasing GPU resources. Replay and calibrate are pure CPU workloads.

## Output Files

Each experiment produces:

```
<experimentId>/
  exp-config.yaml           # Experiment configuration
  vllm_logging.json         # vLLM logging config
  vllm.log                  # vLLM server logs
  observe/
    header.yaml             # TraceV2 header (metadata)
    data.csv                # TraceV2 data (request traces)
    workload.yaml           # BLIS workload spec used
    stdout.log, stderr.log  # Observe phase logs
  replay/
    sim_result.json         # SimResult (simulated latencies)
    stdout.log, stderr.log  # Replay phase logs
  calibrate/
    calibration_report.json # Calibration metrics (MAPE, PearsonR, Quality)
    stdout.log, stderr.log  # Calibrate phase logs
```

## Configuration

### Latency Model

The replay phase uses a latency model to predict per-request latencies. Default is `trained-physics` (best balance of accuracy/simplicity, no per-model training needed). Configure in `values.yaml`:

```yaml
orc:
  latency_model: trained-physics  # or linear, custom, etc.
  gpu_type: H100                   # H100, A100, L40S
```

### Workload Specification

Workloads are defined in BLIS native format under `workload.orcSpec` in `values.yaml`:

```yaml
workload:
  harness: orc
  orcSpec:
    model: MODEL              # Placeholder, replaced at runtime
    endpoint: ENDPOINT        # Placeholder, replaced at runtime
    streaming: true
    arrival:
      process: poisson        # Request arrival pattern
      stages:
        - rate: 8.0           # Requests per second
          duration: 600s
        - rate: 20.0
          duration: 600s
    prompt_tokens:
      distribution: lognormal
      mean: 547
      stddev: 164
    decode_tokens:
      distribution: lognormal
      mean: 248
      stddev: 74
```

## Usage

### 1. Compile the pipeline

```bash
python tektonc/tektonc.py \
  -t tektoncsample/blis-orc/data_pipeline.yaml.j2 \
  -f tektoncsample/blis-orc/values.yaml \
  -o output/blis-orc-pipeline.yaml \
  --explain
```

### 2. Deploy Tekton tasks

```bash
# Deploy ORC-specific tasks
kubectl apply -f tekton/tasks/install-blis.yaml
kubectl apply -f tekton/tasks/orc-observe.yaml
kubectl apply -f tekton/tasks/orc-replay.yaml
kubectl apply -f tekton/tasks/orc-calibrate.yaml

# Deploy shared tasks (if not already deployed)
for task in tekton/tasks/{download-model,create-exp-config,deploy-model,delete-model,upload-s3}.yaml; do
  kubectl apply -f $task
done
```

### 3. Run the pipeline

```bash
kubectl apply -f output/blis-orc-pipeline.yaml
kubectl apply -f tektoncsample/blis-orc/pipelinerun.yaml

# Monitor
tkn pr logs blis-orc-run -f
```

## Calibration Quality Metrics

The calibrate phase produces three metrics for each latency type (TTFT, E2E):

- **MAPE** (Mean Absolute Percentage Error): Lower is better, <20% is excellent
- **Pearson R**: Correlation coefficient, >0.8 is excellent
- **Quality**: Human-readable rating (excellent/good/fair/poor)

### Interpreting Results

| MAPE | Pearson R | Quality | Interpretation |
|------|-----------|---------|----------------|
| <10% | >0.9 | Excellent | Simulator highly accurate |
| 10-20% | 0.8-0.9 | Good | Reliable for most use cases |
| 20-30% | 0.6-0.8 | Fair | Acceptable for rough estimates |
| >30% | <0.6 | Poor | Simulator needs improvement |

**Note**: Poor calibration is NOT a pipeline failure—it's a scientific result indicating the simulator needs tuning.

## Error Handling

- **Observe failure**: Full pipeline retry (same as today—GPU resource was held)
- **Replay/calibrate failure**: Download partial results (trace data still valuable for offline retry)
- **Poor calibration quality**: NOT a failure, logged prominently as scientific result

## Campaign Integration

To use ORC in campaign mode, add experiments with `"harness": "orc"` to `experiments.json`:

```json
{
  "id": 100,
  "model": "Llama-3.1-8B",
  "precision": "FP16",
  "hw": "H100",
  "workload": "general",
  "mbt": 2048,
  "tp": 1,
  "dp": null,
  "safe": "safe",
  "done": false,
  "harness": "orc",
  "latency_model": "trained-physics"
}
```

The campaign runner will automatically select the ORC template and build appropriate values.
