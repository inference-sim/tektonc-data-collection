# BLIS Inference-Perf

Automated LLM benchmarking pipeline using the **inference-perf** harness on Tekton. Provides detailed per-request lifecycle metrics for deep performance analysis.

## Quick Start

```bash
# Basic usage (will prompt for details)
/blis-inference-perf llama-2-7b

# With workload profile
/blis-inference-perf llama-2-7b general

# Full specification
/blis-inference-perf llama-2-7b general in diya with TP=1

# Sweep mode (linear rate sweeping)
/blis-inference-perf llama-2-7b general sweep
```

## Key Features

### Detailed Metrics
- **Per-request lifecycle**: Token-to-token latency, TTFT, E2E timing
- **Stage metrics**: Prefill/decode stage breakdowns
- **Layer-level timing**: Per-layer execution analysis

### Built-in Observability
- Journey tracing (<1% overhead)
- Step tracing at 10% sampling (5-8% overhead)
- KV cache event tracking (3-5% overhead)

### Workload Profiles
- **general**: Balanced workload (8.0→20.0 req/s, 45 clients)
- **codegen**: Code completion (5.0→10.0 req/s, 44 clients)
- **roleplay**: Long conversations (6.0 req/s steady, 50 clients)
- **reasoning**: Decode-heavy (4.0 req/s steady, 23 clients, 1448 output tokens)

## vs blis-data-collector

| Aspect | blis-data-collector | blis-inference-perf-collector |
|--------|---------------------|-------------------------------|
| **Harness** | guidellm | inference-perf |
| **Focus** | Sweep-based benchmarking | Request lifecycle analysis |
| **Metrics** | Aggregate stats | Per-request + stage metrics |
| **Workloads** | GuideLLM profiles | YAML profileTemplate |
| **Output** | guidellm-results.json | lifecycle_metrics.json (multiple) |

## Output Files

Results are saved to `results/<experiment-id>/`:

```
results/20260217-121756-llama-2-7b-inference-perf/
├── values.yaml                                 # Configuration used
├── pipeline.yaml                               # Generated Tekton pipeline
├── pipelinerun.yaml                           # Generated PipelineRun
├── results/
│   ├── per_request_lifecycle_metrics.json     # Per-request timings
│   ├── stage_0_lifecycle_metrics.json         # Stage 1 metrics
│   ├── stage_1_lifecycle_metrics.json         # Stage 2 metrics (if multi-stage)
│   └── summary_lifecycle_metrics.json         # Aggregated summary
├── traces.json                                # OTEL traces
└── kv_events.jsonl                            # KV cache events
```

## Prerequisites

1. **Kubernetes cluster** with Tekton installed
2. **CLI tools**: `kubectl`, `tkn`
3. **Resources**:
   - GPU nodes (NVIDIA H100s)
   - Namespace with secrets: `hf-secret`, `s3-secret`
   - PVCs: `model-pvc`, `data-pvc`

## Examples

### Standard Benchmarking

```bash
# General workload with configured load stages
/blis-inference-perf llama-2-7b general

# Code completion workload
/blis-inference-perf llama3-8b codegen in diya

# Reasoning workload (decode-heavy)
/blis-inference-perf llama3-8b reasoning in diya with TP=2
```

### Sweep Mode

Replace workload's load stages with linear rate sweeping:

```bash
# Sweep general workload
/blis-inference-perf llama-2-7b general sweep

# Sweep codegen data with rate variations
/blis-inference-perf llama3-8b codegen sweep in diya
```

## Monitoring

The skill automatically launches background monitoring. You can also check status manually:

```bash
# Watch logs in real-time
tkn pr logs <experiment-id> -n diya -f

# Check status
tkn pr describe <experiment-id> -n diya

# View results
ls -la results/<experiment-id>/
```

## Analysis Tips

```bash
# View summary metrics
jq '.' results/<experiment-id>/results/summary_lifecycle_metrics.json

# Extract per-request TTFT
jq '.requests[].ttft' results/<experiment-id>/results/per_request_lifecycle_metrics.json

# Trace span types
jq '.resourceSpans[].scopeSpans[].spans[].name' results/<experiment-id>/traces.json | sort | uniq -c

# KV event distribution
cat results/<experiment-id>/kv_events.jsonl | jq -r '.[1][][0]' | sort | uniq -c
```

## Template Files

- `data_pipeline.yaml.j2`: Main Tekton pipeline template with loop constructs
- `values.yaml`: Default configuration (models, resources, observability)
- `pipeline.yaml`: Compiled pipeline (generated during setup)
- `pipelinerun.yaml`: Pipeline run instance (generated during setup)

## Customization

To modify defaults, edit `values.yaml` in this directory before running the skill. Common changes:

```yaml
# vLLM settings
vllm:
  tensorParallel: 2
  gpuMemoryUtilization: 0.9

# Observability (all enabled by default)
tracing:
  enabled: true
  stepSampleRate: 0.1
  logKvEvents: true

# Workload (controlled by workloads.yaml)
workload:
  profileTemplate:
    load: ...
    data: ...
```

---

**See Also:**
- [workloads.yaml](../../workloads.yaml) - Workload profile definitions
- [blis-data-collector](../blis/) - For sweep-based benchmarking with guidellm
