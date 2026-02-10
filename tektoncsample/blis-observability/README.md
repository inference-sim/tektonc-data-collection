# vLLM Observability Example

> **This README is for blis-observability.** For the standard BLIS data collection pipeline (without observability), see [Quick Reference](#quick-reference-standard-blis-pipeline) at the bottom.

This example demonstrates how to enable comprehensive observability features in vLLM deployments through Tekton pipelines:

- **Custom vLLM image** - Use specialized vLLM builds with tracing support
- **Journey tracing** - Per-request OTEL spans tracking full request lifecycle
- **Step tracing** - Scheduler step metrics exported as OTEL spans
- **KV cache events** - Block-level cache operations streamed to JSONL

## BLIS vs BLIS-Observability

| Feature | **blis** (`tektoncsample/blis/`) | **blis-observability** (this directory) |
|---------|----------------------------------|----------------------------------------|
| **Purpose** | Production benchmarking | Debugging and deep analysis |
| **Output** | Performance metrics, throughput | OTEL traces, KV cache events |
| **Use cases** | Compare configurations, collect data | Debug latency, analyze scheduler |
| **Overhead** | Minimal | 8-12% (with full tracing) |
| **Namespace** | Configurable (e.g., `mert`) | `diya` (recommended) |

**Choose blis-observability when you need to:**
- Understand per-request latency breakdown
- Analyze KV cache behavior and offloading patterns
- Debug scheduler decisions and batch formation
- Optimize memory usage

---

## Quick Start (BLIS-Observability)

> **Note:** All instructions below are for `blis-observability` pipeline in the `diya` namespace.

### Prerequisites
- Tekton installed on your Kubernetes cluster
- `tkn` CLI installed
- Namespace `diya` with PVCs: `model-pvc` (300Gi), `data-pvc` (300Gi)
- Secrets: `hf-secret` (HuggingFace token), `s3-secret` (S3 credentials)
- Service account `helm-installer` with appropriate permissions

### Compile and Deploy (blis-observability)

```bash
# 1. Compile the OBSERVABILITY pipeline
python tektonc/tektonc.py \
  -t tektoncsample/blis-observability/pipeline.yaml.j2 \
  -f tektoncsample/blis-observability/values.yaml \
  -o /tmp/observability-pipeline.yaml \
  --explain

# 2. Deploy Tekton tasks (first time only)
for task in tekton/tasks/*.yaml; do
  kubectl apply -f "$task" -n diya
done

# 3. Apply the compiled pipeline
kubectl apply -f /tmp/observability-pipeline.yaml -n diya

# 4. Create and run the pipelinerun
kubectl create -f tektoncsample/blis-observability/pipelinerun.yaml -n diya

# 5. Monitor progress
tkn pr list -n diya
tkn pr logs -f vllm-observability-run -n diya
```

### What Gets Deployed (blis-observability)

The observability pipeline executes these tasks in sequence:
1. **download-model** - Downloads facebook/opt-125m from HuggingFace
2. **install-guidellm** - Installs the guidellm workload generator
3. **create-exp-config** - Creates experiment configuration
4. **deploy-otel-collector** - Deploys OpenTelemetry collector for trace collection
5. **deploy-model** - Deploys vLLM with observability features enabled (custom image + tracing)
6. **run-workload** - Runs guidellm workload to generate observability data
7. **collect-kv-events** - Collects KV cache events from vLLM pod
8. **delete-model/delete-otel-collector** - Cleanup

### Output (blis-observability)

Results are stored in `data-pvc` under `/obs-exp-001/`:
- `traces.json` - OTEL traces (journey + step tracing)
- `kv_events.jsonl` - KV cache events
- `kv_events_summary.txt` - Event type counts
- Guidellm benchmark results

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                  vllm-model Pod                         │
│  ┌─────────────────────┐  ┌──────────────────────────┐ │
│  │   vllm container    │  │ kv-events-subscriber     │ │
│  │   (custom image)    │  │ (sidecar)                │ │
│  │                     │  │                          │ │
│  │  Journey tracing ───┼──┼─► OTLP                   │ │
│  │  Step tracing ──────┼──┼─► OTLP                   │ │
│  │  KV events ─────────┼──┼─► ZMQ → JSONL           │ │
│  └─────────┬───────────┘  └────────────┬─────────────┘ │
│            │                           │                │
└────────────┼───────────────────────────┼────────────────┘
             │                           │
             ▼                           ▼
┌────────────────────────┐    ┌─────────────────────────┐
│   otel-collector       │    │       data-pvc          │
│   (per-experiment)     │    │                         │
│                        │    │  traces.json            │
│   File exporter ───────┼────►  kv_events.jsonl       │
└────────────────────────┘    └─────────────────────────┘
```

## What Each Tracing Type Captures

### Journey Tracing
**What:** End-to-end request lifecycle spans
**Span name:** `llm_core`
**Contains:**
- Request arrival time
- Time to first token (TTFT)
- Total generation time
- Model name, temperature, sampling params

**Use case:** Understand per-request latency and throughput

### Step Tracing
**What:** Scheduler execution metrics per batch
**Span name:** `scheduler_steps_N` (where N increments)
**Contains:**
- Batch size
- Number of running sequences
- Number of waiting sequences
- GPU KV cache utilization
- Preemptions (if any)

**Use case:** Understand scheduler behavior, batch formation, and KV cache pressure

### KV Cache Events
**What:** Block-level cache operations
**Event types:**
- `BlockStored` - New KV block added to cache
- `BlockRemoved` - KV block evicted from cache
- `CacheStoreCommitted` - Scheduler commits to offload blocks to CPU
- `CacheLoadCommitted` - Scheduler commits to load blocks from CPU
- `TransferInitiated` / `TransferCompleted` - GPU↔CPU DMA transfers
- `CacheEviction` - CPU cache eviction

**Use case:** Debug KV cache behavior, analyze offloading patterns, optimize memory

## Configuration (blis-observability)

### Custom vLLM Image

Specify the image in `tektoncsample/blis-observability/values.yaml`:

```yaml
stack:
  vllm:
    image: "ghcr.io/inference-sim/vllm:0.6.8"
```

This gets converted to a Helm override in the pipeline:
```yaml
- decode.containers[name="vllm"].image=ghcr.io/inference-sim/vllm:0.6.8
```

**Requirements:**
- vLLM 0.6.0+ for KV events subscriber script
- Custom builds should include `examples/online_serving/kv_events_subscriber.py`

### Tracing Configuration

Enable/disable features in `values.yaml`:

```yaml
stack:
  tracing:
    # Journey tracing
    journey: true

    # Step tracing
    step:
      enabled: true
      sample_rate: 0.1              # Sample 10% of batches
      rich_subsample_rate: 0.1      # 10% of samples get full details
      closure_interval: 10          # Export spans every 10 steps

    # KV events
    kv_events:
      enabled: true
      publisher: "zmq"
      offloading_size: 8.0          # 8 GiB CPU memory for offloading
```

### Sampling Recommendations

| Scenario | Journey | Step Rate | Rich Rate | Closure |
|----------|---------|-----------|-----------|---------|
| **Production** | true | 0.01 | 0.01 | 100 |
| **Debug** | true | 0.1 | 0.1 | 10 |
| **Development** | true | 1.0 | 1.0 | 1 |

Higher sampling = more overhead. Start conservative, increase if needed.

## Detailed Usage (blis-observability)

### 1. Compile the Pipeline

```bash
python tektonc/tektonc.py \
  -t tektoncsample/blis-observability/pipeline.yaml.j2 \
  -f tektoncsample/blis-observability/values.yaml \
  -o /tmp/observability-pipeline.yaml \
  --explain
```

The `--explain` flag shows which tasks will run and their dependencies.

### 2. Verify Generated Configuration

Check the compiled pipeline includes:

```bash
# Should see custom image
grep "ghcr.io/inference-sim/vllm:0.6.8" /tmp/observability-pipeline.yaml

# Should see tracing args
grep "enable-journey-tracing" /tmp/observability-pipeline.yaml
grep "step-tracing-enabled" /tmp/observability-pipeline.yaml
grep "kv-events-config" /tmp/observability-pipeline.yaml

# Should see sidecar container
grep "kv-events-subscriber" /tmp/observability-pipeline.yaml

# List all unique task references (use -A1 to capture the line after taskRef:)
grep -A1 "taskRef:" /tmp/observability-pipeline.yaml | grep "name:" | sed 's/.*name: //' | sort -u
# Expected output: collect-kv-events, create-otel-collector, delete-model, delete-otel-collector,
#                  deploy-model, download-model, install-inference-perf, run-workload-inference-perf
```

### 3. Deploy Tasks (First Time Only)

```bash
# Deploy all Tekton tasks to diya namespace
for task in tekton/tasks/*.yaml; do
  kubectl apply -f "$task" -n diya
done

# Deploy step actions if needed
for step in tekton/steps/*.yaml; do
  kubectl apply -f "$step" -n diya
done
```

### 4. Review and Customize PipelineRun

The example includes a `pipelinerun.yaml` file. Review and customize it for your environment:

```bash
# Edit the pipelinerun.yaml to match your cluster
vim tektoncsample/blis-observability/pipelinerun.yaml
```

Key parameters to customize:
- `experimentId` - Unique identifier for this run
- `model` - Model to deploy (e.g., facebook/opt-125m)
- `namespace` - Kubernetes namespace (use `diya` for blis-observability experiments)
- PVC names (`model-pvc`, `data-pvc`)
- Secret names (`hf-secret`, `s3-secret`)

### 5. Run the Pipeline

```bash
# Apply pipeline (to diya namespace)
kubectl apply -f /tmp/observability-pipeline.yaml -n diya

# Create and run
kubectl create -f tektoncsample/blis-observability/pipelinerun.yaml -n diya

# Monitor (in diya namespace)
tkn pr list -n diya
tkn pr logs -f -L -n diya
```

### 6. Extract Output Files

Once the pipeline completes:

```bash
# Find the data PVC pod in diya namespace (or use pvc-debug pod if available)
kubectl get pods -n diya

# Extract OTEL traces
kubectl cp -n diya <pod>:/workspace/data/obs-exp-001/traces.json ./traces.json

# Extract KV events
kubectl cp -n diya <pod>:/workspace/data/obs-exp-001/kv_events.jsonl ./kv_events.jsonl

# If using collect-kv-events task, also extract summary
kubectl cp -n diya <pod>:/workspace/data/obs-exp-001/kv_events_summary.json ./kv_events_summary.json
```

## Analyzing Output (blis-observability)

### Journey and Step Traces

View all span names:
```bash
jq '.resourceSpans[].scopeSpans[].spans[].name' traces.json | sort | uniq -c
```

Expected output:
```
  50  llm_core              # Journey tracing (one per request)
   8  scheduler_steps_1     # Step tracing spans
   7  scheduler_steps_2
   ...
```

Extract journey span details:
```bash
jq '.resourceSpans[].scopeSpans[].spans[] | select(.name == "llm_core")' traces.json | head -1
```

Extract step tracing metrics:
```bash
jq '.resourceSpans[].scopeSpans[].spans[] | select(.name | startswith("scheduler_steps"))' traces.json
```

### KV Cache Events

Count event types:
```bash
cat kv_events.jsonl | jq -r '.[1][][0]' | sort | uniq -c
```

Expected output (if offloading triggered):
```
  1523  BlockStored
   892  BlockRemoved
    45  CacheStoreCommitted
    45  TransferInitiated
    44  TransferCompleted
    12  CacheEviction
```

View a specific event:
```bash
# First BlockStored event
cat kv_events.jsonl | jq '.[1][][] | select(.[0] == "BlockStored")' | head -1
```

## Troubleshooting (blis-observability)

### No traces.json file

**Cause:** OTEL collector batches before writing
**Solution:** Wait 15-30 seconds after workload completes

Check collector logs:
```bash
kubectl logs deployment/otel-<experiment-name> -n diya
```

### Empty kv_events.jsonl

**Cause:** Sidecar not connecting to vLLM ZMQ publisher
**Solution:** Check sidecar logs

```bash
kubectl logs deployment/<model-label>-model-decode -c kv-events-subscriber -n diya
```

Verify ZMQ ports are exposed:
```bash
kubectl get svc <model-label>-model-decode -o yaml -n diya | grep -A 5 ports
```

### No KV offloading events

**Cause:** Not enough requests to exceed GPU cache
**Solution:** Increase workload concurrency or sequence length

For opt-125m with 10% GPU memory (~8GB cache):
- GPU KV cache holds ~200K tokens
- Send 300+ concurrent 1000-token requests to trigger offloading

### Custom image not used

**Cause:** Override not applied correctly
**Solution:** Check compiled pipeline

```bash
grep "decode.containers\[name=\"vllm\"\].image" /tmp/observability-pipeline.yaml
```

Verify no typos in values.yaml image path.

## Adapting blis-observability to Your Experiments

### 1. Copy Configuration

```bash
cp -r tektoncsample/blis-observability tektoncsample/my-experiment
```

### 2. Customize values.yaml

```yaml
experiment:
  name: my-custom-tracing-exp

stack:
  vllm:
    image: "my-registry/vllm:custom-tag"

  tracing:
    journey: true
    step:
      enabled: true
      sample_rate: 0.05  # Lower sampling for production
    kv_events:
      enabled: false     # Disable if not needed
```

### 3. Add to Existing Pipelines

Merge tracing configuration into existing `values.yaml`:

```yaml
# Existing configuration
stack:
  model:
    name: "meta-llama/Llama-2-7b"
    configuration: |
      # ... existing config

  # Add tracing section
  vllm:
    image: "ghcr.io/inference-sim/vllm:0.6.8"
  tracing:
    journey: true
    # ... tracing config
```

Then add overrides to your pipeline template's `deploy-model` task following the pattern in `blis-observability/pipeline.yaml.j2`.

## Performance Impact (blis-observability)

| Feature | Overhead | Notes |
|---------|----------|-------|
| Journey tracing | <1% | Minimal, one span per request |
| Step tracing (1% sample) | <2% | Grows with sample rate |
| Step tracing (10% sample) | 5-8% | Acceptable for debugging |
| KV events | 3-5% | ZMQ publishing overhead |
| All features | 8-12% | Combined with 10% step sampling |

**Recommendation:** In production, use journey tracing always, step tracing at 1-5% sampling, and KV events only when debugging cache behavior.

## References

- [testk8s/collectanddebug.yaml](../../testk8s/collectanddebug.yaml) - Standalone K8s manifest with same setup
- [testk8s/README.md](../../testk8s/README.md) - Detailed architecture and troubleshooting
- [OBSERVABILITY_PLAN.md](../../OBSERVABILITY_PLAN.md) - Implementation plan and design decisions

---

## Quick Reference: Standard BLIS Pipeline

For production benchmarking **without observability features**, use the standard BLIS pipeline:

### Location
`tektoncsample/blis/` - Contains `data_pipeline.yaml.j2`, `values.yaml`, and `pipelinerun.yaml`

### Quick Deploy

```bash
# 1. Compile pipeline
python tektonc/tektonc.py \
  -t tektoncsample/blis/data_pipeline.yaml.j2 \
  -f tektoncsample/blis/values.yaml \
  -o /tmp/blis-pipeline.yaml \
  --explain

# 2. Deploy tasks (first time only)
for task in tekton/tasks/*.yaml; do kubectl apply -f "$task"; done

# 3. Apply pipeline
kubectl apply -f /tmp/blis-pipeline.yaml

# 4. Edit pipelinerun.yaml to set your experimentId and model
# 5. Create pipelinerun
kubectl create -f tektoncsample/blis/pipelinerun.yaml

# 6. Monitor
tkn pr list
tkn pr logs -f -L
```

### Key Differences from blis-observability

| Aspect | blis | blis-observability |
|--------|------|-------------------|
| **Template** | `data_pipeline.yaml.j2` | `pipeline.yaml.j2` |
| **Namespace** | Configurable (param) | `diya` (hardcoded in examples) |
| **OTEL Collector** | Yes (basic tracing) | Yes (with file exporter) |
| **Custom vLLM image** | No | Yes (`ghcr.io/inference-sim/vllm:0.6.8`) |
| **Journey tracing** | Yes (basic OTEL) | Yes (configurable) |
| **Step tracing** | No | Yes (with sampling) |
| **KV events** | No | Yes (ZMQ subscriber sidecar) |
| **Output** | Guidellm results | Traces + KV events + Guidellm |

### Configuration

Edit `values.yaml` for:
- **Tensor parallelism treatments**: `stack.treatments.tensorParallelism: [1, 2, 4]`
- **Workload profiles**: chatsweep, codesweep, train, summarization, etc.
- **Model limits**: MAX_MODEL_LEN, MAX_NUM_BATCHED_TOKENS, MAX_NUM_SEQS
- **Upload target**: S3 bucket configuration

### Output Location

Results stored in `data-pvc` under `/<experimentId>-<tp>/`:
- Guidellm benchmark JSON
- Performance metrics
- OTEL traces (basic, no step/KV events)
