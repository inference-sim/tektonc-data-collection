# blis-inference-perf Design Plan

## Objective
Create a new tektoncsample that replaces GuideLLM with inference-perf in BLIS, keeping OTEL tracing and BLIS model deployment patterns.

## What to Keep from BLIS
- ✅ OTEL collector deployment and tracing
- ✅ Tensor parallelism treatments
- ✅ `create-exp-config` task (vllm_logging.json)
- ✅ Model deployment with OTEL configuration
- ✅ Results upload to S3

## What to Remove
- ❌ Training tasks (`train-preprocess`, `train-blis-from-guidellm`)
- ❌ Training data upload
- ❌ GuideLLM-specific flat config structure

## What NOT to Include
- ❌ Gateway/GAIE infrastructure
- ❌ HTTPRoute deployment
- ❌ llm-d-benchmark analysis tooling

## Configuration Format

Use inference-perf's native format in `values.yaml`:

```yaml
experiment:
  name: blis-inference-perf
  description: BLIS with inference-perf harness

stack:
  treatments:
    tensorParallelism: [4]

  MAX_MODEL_LEN: 8192
  MAX_NUM_BATCHED_TOKENS: 4096
  MAX_NUM_SEQS: 128

  model:
    helmValues: { ... }  # Same as BLIS with OTEL config

workload:
  harness: inference-perf

  profileTemplate:
    load:
      type: constant
      stages:
        - rate: 8.0
          duration: 600      # Low traffic period (0-600s)
        - rate: 20.0
          duration: 600      # Peak traffic period (600-1200s)

    api:
      type: completion
      streaming: true

    server:
      type: vllm
      model_name: MODEL
      base_url: STACK_ENDPOINT
      ignore_eos: true

    tokenizer:
      pretrained_model_name_or_path: TOKENIZER

    data:
      type: shared_prefix
      shared_prefix:
        num_unique_system_prompts: 9
        num_users_per_system_prompt: 5
        system_prompt_len: 100
        question_len: 447
        output_len: 248
        enable_multi_turn_chat: true

    report:
      request_lifecycle:
        summary: true
        per_stage: true
        per_request: true

    storage:
      local_storage:
        path: PATH

upload_target:
  type: s3
  configuration:
    bucket: cloud-object-storage-cos-standard-ere
    endpoint: https://s3.us-east.cloud-object-storage.appdomain.cloud
```

## Pipeline Structure

```
1. download-model
2. install-inference-perf-blis (NEW - installs into data workspace like guidellm)
3. Per-stack loop (tensorParallelism):
   a. create-exp-config
   b. deploy-otel-collector
   c. deploy-model
   d. run-workload-inference-perf-blis (NEW - runs from data workspace)
   e. delete-model
   f. delete-otel-collector
   g. raw-upload
4. finally: cleanup (delete model, delete otel)
```

## Workspaces

```yaml
workspaces:
  - name: model-cache
  - name: hf-credentials
  - name: data              # inference-perf installed here (like guidellm)
  - name: target-credentials
```

**No source workspace needed!** Install inference-perf into data workspace like GuideLLM.

## Tasks

**Existing tasks to use:**
- `download-model`
- `create-exp-config` (from BLIS)
- `create-otel-collector` (from BLIS)
- `deploy-model`
- `delete-model`
- `delete-otel-collector`
- `upload-s3`

**New tasks to create:**
- `install-inference-perf-blis` - Install inference-perf into data workspace (modeled after `install-guidellm`)
- `run-workload-inference-perf-blis` - Run inference-perf from data workspace with profileTemplate support

## Installation Approach

Following the GuideLLM pattern:

**install-inference-perf-blis:**
```yaml
workspaces:
  - name: data

script: |
  TARGET="$(workspaces.data.path)/inference-perf"
  pip install --target="$TARGET" git+https://github.com/kubernetes-sigs/inference-perf.git@<commit>
```

**run-workload-inference-perf-blis:**
```yaml
workspaces:
  - name: data
  - name: hf-credentials

script: |
  export PYTHONPATH="$(workspaces.data.path)/inference-perf"
  inference-perf --config_file <profile>
```

This keeps the same workspace structure as BLIS (4 workspaces) while enabling inference-perf.

## ProfileTemplate Handling

The new `run-workload-inference-perf-blis` task needs to:
1. Accept `profileTemplate` as a parameter (from values.yaml)
2. Substitute template placeholders:
   - `MODEL` → actual model name
   - `STACK_ENDPOINT` → model pod URL
   - `TOKENIZER` → model name
   - `PATH` → results directory path
3. Write the resolved profile to a YAML file
4. Run `inference-perf --config_file <profile.yaml>`

This is similar to how `run-workload-inference-perf` (prefix-caching) handles it, but adapted for the data workspace approach.

## Files to Create

**Tekton tasks:**
```
tekton/tasks/
├── install-inference-perf-blis.yaml           # NEW - install into data workspace
└── run-workload-inference-perf-blis.yaml      # NEW - run from data workspace
```

**Tektoncsample:**
```
tektoncsample/blis-inference-perf/
├── pipeline.yaml.j2        # Jinja2 template
├── pipeline.yaml           # Generated example
├── pipelinerun.yaml        # Example execution
└── values.yaml             # Configuration
```

## Template Approach

Base on `prefix-caching` structure, adding BLIS-specific elements:
- Add `create-exp-config` task
- Add OTEL collector deployment
- Keep inference-perf workload format
- Remove gateway/GAIE/HTTPRoute
- Use tensor parallelism loop from BLIS

## Key Differences from Original BLIS

| Aspect | Original BLIS | blis-inference-perf |
|--------|---------------|---------------------|
| Harness | GuideLLM | inference-perf |
| Config Format | Flat params | inference-perf profileTemplate |
| Workspaces | 4 | 4 (same - no source workspace) |
| Installation | install-guidellm → data | install-inference-perf-blis → data |
| Training | Yes | No |
| OTEL | Yes | Yes |
| Gateway/GAIE | No | No |
| New Tasks | 0 | 2 (install + run) |
