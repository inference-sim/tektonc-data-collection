# Plan: Incorporate vLLM Custom Tracing into Tekton Pipelines

## Overview

Add custom vLLM image support, journey tracing, step tracing, and KV events capture from `testk8s/collectanddebug.yaml` into Tekton pipelines.

**Key insight:** Existing `deploy-model.yaml` task already supports everything via Helm overrides - no task modifications needed.

## Implementation Strategy

### 1. Custom vLLM Image
**Where:** `values.yaml` → Helm override in `pipeline.yaml.j2`
```yaml
stack:
  vllm:
    image: "ghcr.io/inference-sim/vllm:0.6.8"  # Custom image with tracing support
```
**Converts to:** `decode.containers[name="vllm"].image=ghcr.io/inference-sim/vllm:0.6.8`

### 2. Journey & Step Tracing
**Where:** `values.yaml` → vLLM args via Helm overrides
```yaml
stack:
  tracing:
    journey: true
    step:
      enabled: true
      sample_rate: 0.1
      rich_subsample_rate: 0.1
      closure_interval: 10
```
**Converts to:**
- `--enable-journey-tracing`
- `--step-tracing-enabled`
- `--step-tracing-sample-rate=0.1`
- `--step-tracing-rich-subsample-rate=0.1`
- `--step-tracing-closure-interval=10`
- `--otlp-traces-endpoint=http://otel-{{ stackModelLabel }}:4318/v1/traces`

**Output:** Traces written to `/workspace/data/<stackModelLabel>/traces.json` by existing OTEL collector

### 3. KV Events Capture
**Where:** Sidecar container in `values.yaml`
```yaml
stack:
  tracing:
    kv_events:
      enabled: true
      offloading_size: 8.0
```

**Components:**
- **vLLM args:** `--kv-events-config={"enable_kv_cache_events": true, "publisher": "zmq", "endpoint": "tcp://*:5557", ...}`
- **ZMQ ports:** 5557 (pub), 5558 (replay)
- **Sidecar container:** Runs `examples/online_serving/kv_events_subscriber.py`
- **Output:** `/workspace/data/<stackModelLabel>/kv_events.jsonl`

**How sidecar works:**
1. vLLM publishes events over ZMQ localhost:5557
2. Sidecar subscribes and writes to shared PVC
3. Both containers mount same data PVC

## Files to Create

### 1. `tektoncsample/blis-observability/values.yaml`
- Custom vLLM image specification
- Tracing configuration (journey, step, KV events)
- Sidecar container for KV events subscriber
- OTEL environment variables

### 2. `tektoncsample/blis-observability/pipeline.yaml.j2`
- Convert `stack.vllm.image` to override
- Convert `stack.tracing.*` to vLLM args
- Add sidecar deployment via Helm overrides
- Configure OTEL endpoint dynamically

### 3. `tektoncsample/blis-observability/README.md`
- How to use custom vLLM images
- What each tracing type captures
- How to extract and view output files

### 4. `tekton/tasks/collect-kv-events.yaml`
- Parse and summarize kv_events.jsonl
- Count event types (BlockStored, CacheStoreCommitted, etc.)
- Validate JSONL format

### 5. Update `CLAUDE.md`
- Add observability section with tracing parameters

## Key Configuration Pattern

**In pipeline.yaml.j2:**
```jinja2
- name: deploy-model
  taskRef: { name: deploy-model }
  params:
    - name: overrides
      value:
        # Custom image
        - decode.containers[name="vllm"].image={{ stack.vllm.image }}

        # Journey tracing
        {% if stack.tracing.journey %}
        - decode.containers[name="vllm"].args=--enable-journey-tracing
        {% endif %}

        # Step tracing
        {% if stack.tracing.step.enabled %}
        - decode.containers[name="vllm"].args=--step-tracing-enabled
        - decode.containers[name="vllm"].args=--step-tracing-sample-rate={{ stack.tracing.step.sample_rate }}
        {% endif %}

        # KV events
        {% if stack.tracing.kv_events.enabled %}
        - decode.containers[name="vllm"].args=--kv-events-config={"enable_kv_cache_events": true, "publisher": "zmq", "endpoint": "tcp://*:5557", "replay_endpoint": "tcp://*:5558", "topic": "kv-events"}
        {% endif %}

        # OTEL endpoint
        - decode.containers[name="vllm"].args=--otlp-traces-endpoint=http://otel-{{ stackModelLabel }}:4318/v1/traces
```

## Reference Files
- `testk8s/collectanddebug.yaml` - Source configuration
- `tektoncsample/blis/data_pipeline.yaml.j2` - Template pattern
- `tekton/tasks/deploy-model.yaml` - Uses overrides (no changes needed)

## Verification

```bash
# 1. Compile template
python tektonc/tektonc.py \
  -t tektoncsample/blis-observability/pipeline.yaml.j2 \
  -f tektoncsample/blis-observability/values.yaml \
  --explain

# 2. Check generated YAML includes:
#    - Custom vLLM image
#    - --enable-journey-tracing
#    - --step-tracing-* flags
#    - --kv-events-config
#    - kv-events-subscriber sidecar container

# 3. After pipeline runs, extract data:
kubectl cp <pod>:/workspace/data/<exp>/traces.json ./
kubectl cp <pod>:/workspace/data/<exp>/kv_events.jsonl ./

# 4. Verify traces contain:
jq '.resourceSpans[].scopeSpans[].spans[].name' traces.json | grep -E "llm_core|scheduler_steps"

# 5. Verify KV events contain:
cat kv_events.jsonl | head -1 | jq '.[1][][0]' | grep -E "BlockStored|CacheStoreCommitted"
```
