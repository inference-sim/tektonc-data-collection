# BLIS Data Collector: Expanded Plan A (Skill-Based Approach)

This document provides a comprehensive implementation plan for the `/blis-data-collector` Claude Code skill, with expanded support for custom vLLM configurations, image injection, and intelligent parameter matching.

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Architecture Overview](#architecture-overview)
3. [Parameter Taxonomy](#parameter-taxonomy)
4. [vLLM Configuration Support](#vllm-configuration-support)
5. [Intelligent Parameter Matching](#intelligent-parameter-matching)
6. [User Interaction Flow](#user-interaction-flow)
7. [Configuration Files](#configuration-files)
8. [Implementation Details](#implementation-details)
9. [Error Handling](#error-handling)

---

## Executive Summary

The `/blis-data-collector` skill automates the 11-step BLIS data collection workflow through:

1. **Natural language parsing** - Extracts experiment parameters from user requests
2. **Intelligent matching** - Maps user requirements to predefined configurations in `values.yaml`
3. **Explicit confirmation** - Verifies all parameters (especially vLLM config) before deployment
4. **Minimal file modification** - Uses temporary files for runtime configuration
5. **Comprehensive vLLM support** - Handles custom images, tags, and arbitrary CLI arguments

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                     /blis-data-collector Skill                      │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌─────────────┐    ┌──────────────┐    ┌────────────────────────┐ │
│  │   Parser    │───▶│   Matcher    │───▶│  Config Generator      │ │
│  │             │    │              │    │                        │ │
│  │ - NL intent │    │ - values.yaml│    │ - Temp pipelinerun.yaml│ │
│  │ - vLLM args │    │ - presets    │    │ - Temp values.yaml     │ │
│  │ - model     │    │ - models     │    │ - Helm overrides       │ │
│  └─────────────┘    └──────────────┘    └────────────────────────┘ │
│         │                  │                       │                │
│         ▼                  ▼                       ▼                │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │                    Confirmation Dialog                        │  │
│  │  - Experiment params  - vLLM image/args  - Workload profile  │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                              │                                      │
│                              ▼                                      │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │                    Execution Engine                           │  │
│  │  - Pre-flight checks  - Deploy  - Monitor  - Data retrieval  │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Parameter Taxonomy

### Tier 1: Core Experiment Parameters (Always Required)

| Parameter | Source | Examples | Default |
|-----------|--------|----------|---------|
| `experimentId` | User or auto-generate | `feb04-llama70b-chatbot` | Auto: `{date}-{model-short}-{workload}` |
| `model` | User (required) | `meta-llama/Llama-2-7b-hf` | None |
| `namespace` | User or kubeconfig | `mert`, `benchmark` | Current kubectl context |

### Tier 2: vLLM Engine Parameters (Frequently Changed)

| Parameter | Location in values.yaml | CLI Equivalent | Default |
|-----------|------------------------|----------------|---------|
| `tensorParallelism` | `stack.treatments.tensorParallelism` | N/A (deployment config) | `[1]` |
| `MAX_MODEL_LEN` | `stack.MAX_MODEL_LEN` | `--max-model-len` | `4096` |
| `MAX_NUM_BATCHED_TOKENS` | `stack.MAX_NUM_BATCHED_TOKENS` | `--max-num-batched-tokens` | `2048` |
| `MAX_NUM_SEQS` | `stack.MAX_NUM_SEQS` | `--max-num-seqs` | `256` |

### Tier 3: vLLM Image Configuration (Advanced)

| Parameter | Location in values.yaml | Default |
|-----------|------------------------|---------|
| `vllm.image` | `stack.model.helmValues.decode.containers[name="vllm"].image` | `vllm/vllm-openai:v0.11.0` |
| `vllm.initImage` | `stack.model.helmValues.decode.initContainers[0].image` | `vllm/vllm-openai:v0.11.0` |

### Tier 4: vLLM Additional Arguments (Custom/Advanced)

These are **arbitrary CLI arguments** passed to vLLM. Examples:

| Argument | Use Case | Image Type |
|----------|----------|------------|
| `--trust-remote-code` | Models requiring custom code | Any |
| `--journey-tracing-sample-rate 1.0` | Journey tracing | Custom vLLM images |
| `--enable-prefix-caching` | Prefix caching experiments | Any |
| `--enforce-eager` | Disable CUDA graphs | Any |
| `--gpu-memory-utilization 0.9` | Memory tuning | Any |
| `--dtype auto` | Precision control | Any |
| `--quantization awq` | Quantized models | Any |

### Tier 5: Workload Configuration

| Parameter | Location | Options |
|-----------|----------|---------|
| `workload.preset` | `stack.workload` | `chatsweep`, `codesweep`, `train`, `summarization`, `chatbot`, `contentgen`, `doc`, `prefilldominant` |
| Custom workload | `stack.workload.profile.*` | User-defined values |

#### Custom Workload Profile Support

Users can specify custom token distributions instead of using presets. The skill should accept:

```
User: "run with custom workload: prompt_tokens=500, output_tokens=200, max_requests=100"
```

**Custom Workload Parameters:**

| Parameter | Description | Default |
|-----------|-------------|---------|
| `prompt_tokens` | Mean prompt token count | Required |
| `prompt_tokens_stdev` | Standard deviation | 0 |
| `prompt_tokens_min` | Minimum prompt tokens | 1 |
| `prompt_tokens_max` | Maximum prompt tokens | prompt_tokens * 2 |
| `output_tokens` | Mean output token count | Required |
| `output_tokens_stdev` | Standard deviation | 0 |
| `output_tokens_min` | Minimum output tokens | 1 |
| `output_tokens_max` | Maximum output tokens | output_tokens * 2 |
| `prefix_tokens` | Cached prefix length | 0 |
| `max_requests` | Total requests to generate | 50 |
| `rate` | Request rate | 12 |
| `rate_type` | "sweep" or "constant" | "sweep" |

**Example Custom Workload Dialog:**

```
User: /blis-data-collector llama-7b with custom workload prompt=500 output=100

Claude: I'll create a custom workload profile. Please confirm or adjust:

CUSTOM WORKLOAD CONFIGURATION
─────────────────────────────
  Rate Type:         sweep
  Max Requests:      50 (default)
  Rate:              12 (default)

  Prompt Tokens:     500 (mean)
    Standard Dev:    0 (no variation - adjust?)
    Min/Max:         1 / 1000

  Output Tokens:     100 (mean)
    Standard Dev:    0 (no variation - adjust?)
    Min/Max:         1 / 200

  Prefix Tokens:     0 (no prefix caching)

Would you like to add variation (stdev) to the token distributions? [Y/n]
Or specify: "stdev prompt=100 output=50"
```

---

## vLLM Configuration Support

### 4.1 Image Injection Architecture

The skill supports three levels of vLLM image customization:

#### Level 1: Tag Override (Most Common)
```
User: "use vLLM version 0.8.0"
Result: vllm/vllm-openai:v0.8.0
```

#### Level 2: Full Image Override
```
User: "use custom image ghcr.io/myorg/vllm-custom:journey-v1"
Result: ghcr.io/myorg/vllm-custom:journey-v1
```

#### Level 3: Separate Main/Init Images
```
User: "use ghcr.io/myorg/vllm-custom:v1 for main container and keep default for init"
Result:
  - Main: ghcr.io/myorg/vllm-custom:v1
  - Init: vllm/vllm-openai:v0.11.0
```

### 4.2 vLLM Arguments Injection

Arguments are injected via the pipeline template's `overrides` mechanism:

```yaml
# Current overrides in data_pipeline.yaml.j2 (lines 108-115)
- name: overrides
  value:
    - decode.parallelism.tensor={{ tp }}
    - decode.containers[name="vllm"].args=--max-model-len={{ stack.MAX_MODEL_LEN }}
    - decode.containers[name="vllm"].args=--max-num-batched-tokens={{ stack.MAX_NUM_BATCHED_TOKENS }}
    - decode.containers[name="vllm"].args=--max-num-seqs={{ stack.MAX_NUM_SEQS }}
    - decode.containers[name="vllm"].args=--otlp-traces-endpoint=http://otel-{{stackModelLabel}}:4318/v1/traces
    # ... env vars
```

**Extended override support** - The skill will generate additional overrides for:

```yaml
# User-specified additional args
- decode.containers[name="vllm"].args=--journey-tracing-sample-rate=1.0
- decode.containers[name="vllm"].args=--trust-remote-code
- decode.containers[name="vllm"].args=--enable-prefix-caching

# User-specified image override
- decode.containers[name="vllm"].image=ghcr.io/myorg/vllm-custom:journey-v1
- decode.initContainers[0].image=ghcr.io/myorg/vllm-custom:journey-v1
```

### 4.3 Known vLLM Argument Categories

The skill should recognize and categorize vLLM arguments:

```yaml
# .claude/config/vllm-args.yaml
categories:
  performance:
    - name: --max-model-len
      type: integer
      description: Maximum sequence length
      default_source: stack.MAX_MODEL_LEN
    - name: --max-num-batched-tokens
      type: integer
      description: Maximum tokens in a batch
      default_source: stack.MAX_NUM_BATCHED_TOKENS
    - name: --max-num-seqs
      type: integer
      description: Maximum concurrent sequences
      default_source: stack.MAX_NUM_SEQS
    - name: --gpu-memory-utilization
      type: float
      description: GPU memory fraction to use
      default: 0.9

  tracing:
    - name: --otlp-traces-endpoint
      type: string
      description: OTLP endpoint for traces
      auto_configured: true  # Set by pipeline based on OTEL collector
    - name: --journey-tracing-sample-rate
      type: float
      description: Sample rate for journey tracing (custom vLLM only)
      requires_custom_image: true

  model_loading:
    - name: --trust-remote-code
      type: flag
      description: Allow custom model code execution
    - name: --dtype
      type: enum
      values: [auto, half, float16, bfloat16, float, float32]
      description: Data type for model weights
    - name: --quantization
      type: enum
      values: [awq, gptq, squeezellm, fp8]
      description: Quantization method

  optimization:
    - name: --enable-prefix-caching
      type: flag
      description: Enable automatic prefix caching
    - name: --enforce-eager
      type: flag
      description: Disable CUDA graph optimization
    - name: --enable-chunked-prefill
      type: flag
      description: Enable chunked prefill
```

---

## Intelligent Parameter Matching

### 5.1 Matching Strategy

The skill should minimize configuration changes by matching user requirements to existing `values.yaml` settings.

```
┌────────────────────────────────────────────────────────────────────┐
│                    Parameter Matching Flow                          │
├────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  User Request: "llama-7b with TP=2 for chatbot workload"           │
│                                                                     │
│         │                                                           │
│         ▼                                                           │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │ Step 1: Load Current values.yaml                             │   │
│  │   tensorParallelism: [2, 4]  ◀─── User wants 2, already in! │   │
│  │   MAX_MODEL_LEN: 4096                                        │   │
│  │   MAX_NUM_SEQS: 256                                          │   │
│  │   workload.app: chatsweep    ◀─── "chatbot" maps to this    │   │
│  │   image: vllm/vllm-openai:v0.11.0                            │   │
│  └─────────────────────────────────────────────────────────────┘   │
│         │                                                           │
│         ▼                                                           │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │ Step 2: Compare & Identify Matches                           │   │
│  │   ✓ tensorParallelism: 2 exists in [2, 4] → NO CHANGE       │   │
│  │   ✓ workload: chatsweep matches "chatbot" → NO CHANGE       │   │
│  │   ✓ vLLM image: default requested → NO CHANGE               │   │
│  │   ✓ vLLM args: none specified → NO CHANGE                   │   │
│  └─────────────────────────────────────────────────────────────┘   │
│         │                                                           │
│         ▼                                                           │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │ Step 3: Report to User                                       │   │
│  │   "Your request matches current values.yaml configuration.   │   │
│  │    Only pipelinerun.yaml params will be modified:            │   │
│  │      - experimentId: feb04-llama7b-chatbot                   │   │
│  │      - model: meta-llama/Llama-2-7b-hf                       │   │
│  │      - namespace: mert"                                      │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
└────────────────────────────────────────────────────────────────────┘
```

### 5.2 Match Categories

The skill classifies each parameter into one of three categories:

| Category | Description | Action |
|----------|-------------|--------|
| **MATCH** | User value exists in values.yaml | No change needed |
| **SUBSET** | User wants subset of values.yaml options | Filter tensorParallelism array |
| **OVERRIDE** | User value differs from values.yaml | Generate temp override |

### 5.3 Matching Examples

**Example 1: Full Match (No File Changes)**
```
User: "run chatsweep workload with TP=2"
values.yaml: tensorParallelism: [2, 4], workload.app: chatsweep

Result:
  - tensorParallelism: MATCH (2 is in [2, 4])
  - workload: MATCH (chatsweep)
  - Action: Only modify pipelinerun.yaml params
```

**Example 2: Subset Match (Filter Array)**
```
User: "run with only TP=4"
values.yaml: tensorParallelism: [2, 4]

Result:
  - tensorParallelism: SUBSET (user wants [4] from [2, 4])
  - Action: Generate temp values.yaml with tensorParallelism: [4]
```

**Example 3: Override Required**
```
User: "use max-model-len 8192"
values.yaml: MAX_MODEL_LEN: 4096

Result:
  - MAX_MODEL_LEN: OVERRIDE (8192 != 4096)
  - Action: Generate temp values.yaml with MAX_MODEL_LEN: 8192
```

**Example 4: Custom vLLM Image**
```
User: "use custom vLLM image ghcr.io/myorg/vllm:journey-v1 with --journey-tracing-sample-rate 1.0"
values.yaml: image: vllm/vllm-openai:v0.11.0

Result:
  - image: OVERRIDE
  - args: OVERRIDE (new arg)
  - Action: Generate temp values.yaml with:
    - decode.containers[name="vllm"].image: ghcr.io/myorg/vllm:journey-v1
    - decode.initContainers[0].image: ghcr.io/myorg/vllm:journey-v1
    - Additional args override in pipeline
```

### 5.4 Workload Preset Mapping

Map natural language to predefined workloads in `values.yaml`:

| User Input | Matched Preset | Notes |
|------------|----------------|-------|
| "chatbot", "chat", "conversation" | `chatsweep` | Short prompts, medium outputs |
| "code", "coding", "programming", "completion" | `codesweep` | Long prompts, short outputs |
| "training", "fine-tuning" | `train` | Variable lengths |
| "summary", "summarization", "tldr" | `summarization` | Long prompts, medium outputs |
| "prefill heavy", "long context" | `prefilldominant` | Very long prompts, short outputs |
| "content", "generation", "writing" | `contentgen` | Medium prompts, long outputs |
| "document", "long form" | `doc` | Very long prompts and outputs |

---

## User Interaction Flow

### 6.1 Complete Interaction Sequence

```
┌─────────────────────────────────────────────────────────────────────┐
│ STEP 1: USER REQUEST                                                │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│ User: /blis-data-collector I want to run a chatbot benchmark using  │
│       llama-70b with TP=4, and I need to use a custom vLLM image    │
│       with journey tracing enabled                                  │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│ STEP 2: INITIAL PARSING                                             │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│ Claude: I've parsed your request. Here's what I detected:           │
│                                                                     │
│ ┌─────────────────────────────────────────────────────────────────┐ │
│ │ DETECTED PARAMETERS                                             │ │
│ │ ─────────────────────                                           │ │
│ │ Model:          meta-llama/Llama-3.3-70B-Instruct               │ │
│ │ Workload:       chatsweep (matched from "chatbot")              │ │
│ │ Tensor Parallel: [4]                                            │ │
│ │                                                                 │ │
│ │ ⏱ MODEL SIZE WARNING:                                           │ │
│ │   This is a 140GB model. If not already cached on model-pvc,    │ │
│ │   the download-model task will take 30-60 minutes.              │ │
│ │                                                                 │ │
│ │ vLLM Customization Detected:                                    │ │
│ │   • Custom image requested                                      │ │
│ │   • Journey tracing mentioned                                   │ │
│ └─────────────────────────────────────────────────────────────────┘ │
│                                                                     │
│ I need additional details for the vLLM configuration:               │
│                                                                     │
│ 1. What is the custom vLLM image? (e.g., ghcr.io/org/vllm:tag)     │
│ 2. What namespace should I use?                                     │
│ 3. Any specific experiment ID, or should I auto-generate?           │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│ STEP 3: USER PROVIDES DETAILS                                       │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│ User: Use image ghcr.io/ibm/vllm-journey:v0.8.0, namespace is mert, │
│       auto-generate the ID. Also add --journey-tracing-sample-rate  │
│       1.0 to vLLM args                                              │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│ STEP 4: CONFIGURATION COMPARISON                                    │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│ Claude: I've compared your requirements with the current            │
│ values.yaml configuration:                                          │
│                                                                     │
│ ┌─────────────────────────────────────────────────────────────────┐ │
│ │ PARAMETER MATCHING RESULTS                                      │ │
│ │ ─────────────────────────────                                   │ │
│ │                                                                 │ │
│ │ ✓ MATCHES (no changes needed):                                  │ │
│ │   • Workload: chatsweep (already configured)                    │ │
│ │   • MAX_MODEL_LEN: 4096 (using default)                         │ │
│ │   • MAX_NUM_BATCHED_TOKENS: 2048 (using default)                │ │
│ │   • MAX_NUM_SEQS: 256 (using default)                           │ │
│ │                                                                 │ │
│ │ ⚡ SUBSET (will filter):                                         │ │
│ │   • tensorParallelism: [4] (from available [2, 4])              │ │
│ │                                                                 │ │
│ │ ⚠ OVERRIDES (will be modified):                                 │ │
│ │   • vLLM Image: ghcr.io/ibm/vllm-journey:v0.8.0                 │ │
│ │     (was: vllm/vllm-openai:v0.11.0)                             │ │
│ │   • Additional vLLM args: --journey-tracing-sample-rate 1.0     │ │
│ │     (new argument)                                              │ │
│ └─────────────────────────────────────────────────────────────────┘ │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│ STEP 5: EXPLICIT vLLM VERIFICATION                                  │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│ Claude: Please verify the vLLM configuration:                       │
│                                                                     │
│ ┌─────────────────────────────────────────────────────────────────┐ │
│ │ vLLM CONTAINER CONFIGURATION                                    │ │
│ │ ────────────────────────────────                                │ │
│ │                                                                 │ │
│ │ Image (main container):  ghcr.io/ibm/vllm-journey:v0.8.0       │ │
│ │ Image (init container):  ghcr.io/ibm/vllm-journey:v0.8.0       │ │
│ │                                                                 │ │
│ │ Arguments:                                                      │ │
│ │   --max-model-len 4096            (from values.yaml)            │ │
│ │   --max-num-batched-tokens 2048   (from values.yaml)            │ │
│ │   --max-num-seqs 256              (from values.yaml)            │ │
│ │   --otlp-traces-endpoint ...      (auto-configured)             │ │
│ │   --journey-tracing-sample-rate 1.0  ⚠ NEW                      │ │
│ │                                                                 │ │
│ │ Environment Variables:                                          │ │
│ │   OTEL_* variables                (from values.yaml)            │ │
│ │   PYTHONPATH=/deps                (from values.yaml)            │ │
│ │                                                                 │ │
│ │ Resources:                                                      │ │
│ │   Memory: 128Gi (limit/request)                                 │ │
│ │   CPU: 32 (limit/request)                                       │ │
│ │   GPUs: 4 (via TP=4)                                            │ │
│ └─────────────────────────────────────────────────────────────────┘ │
│                                                                     │
│ Do you want to modify any vLLM settings? [Y/n]                      │
│                                                                     │
│ Common modifications:                                               │
│   • Add --trust-remote-code (for custom model code)                 │
│   • Add --enable-prefix-caching (for prefix caching experiments)    │
│   • Change --gpu-memory-utilization (default: 0.9)                  │
│   • Add --dtype bfloat16 (for precision control)                    │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│ STEP 6: USER CONFIRMS OR MODIFIES                                   │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│ User: Looks good, but also add --trust-remote-code                  │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│ STEP 7: FINAL CONFIRMATION                                          │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│ Claude: Here is the complete experiment configuration:              │
│                                                                     │
│ ╔═════════════════════════════════════════════════════════════════╗ │
│ ║           BLIS EXPERIMENT: feb04-llama70b-chatsweep-tp4         ║ │
│ ╠═════════════════════════════════════════════════════════════════╣ │
│ ║                                                                 ║ │
│ ║  CORE SETTINGS                                                  ║ │
│ ║  ─────────────                                                  ║ │
│ ║  Experiment ID:    feb04-llama70b-chatsweep-tp4                 ║ │
│ ║  Namespace:        mert                                         ║ │
│ ║  Model:            meta-llama/Llama-3.3-70B-Instruct            ║ │
│ ║                                                                 ║ │
│ ║  WORKLOAD                                                       ║ │
│ ║  ────────                                                       ║ │
│ ║  Type:             chatsweep                                    ║ │
│ ║  Max Requests:     50                                           ║ │
│ ║  Rate:             12 (sweep)                                   ║ │
│ ║  Prompt Tokens:    70 (σ=35, range: 2-200)                      ║ │
│ ║  Output Tokens:    215 (σ=80, range: 1-512)                     ║ │
│ ║  Prefix Tokens:    284                                          ║ │
│ ║                                                                 ║ │
│ ║  vLLM ENGINE                                                    ║ │
│ ║  ───────────                                                    ║ │
│ ║  Image:            ghcr.io/ibm/vllm-journey:v0.8.0             ║ │
│ ║  Tensor Parallel:  4                                            ║ │
│ ║  Max Model Len:    4096                                         ║ │
│ ║  Max Batch Tokens: 2048                                         ║ │
│ ║  Max Num Seqs:     256                                          ║ │
│ ║  Additional Args:  --journey-tracing-sample-rate 1.0            ║ │
│ ║                    --trust-remote-code                          ║ │
│ ║                                                                 ║ │
│ ║  CHANGES FROM values.yaml                                       ║ │
│ ║  ────────────────────────                                       ║ │
│ ║  • tensorParallelism: [2, 4] → [4]                              ║ │
│ ║  • vLLM image: CHANGED                                          ║ │
│ ║  • vLLM args: +2 additional arguments                           ║ │
│ ║                                                                 ║ │
│ ╚═════════════════════════════════════════════════════════════════╝ │
│                                                                     │
│ Proceed with deployment? [Y/n]                                      │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│ STEP 8: EXECUTION                                                   │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│ User: Y                                                             │
│                                                                     │
│ Claude: Starting BLIS data collection pipeline...                   │
│                                                                     │
│ [1/7] Pre-flight checks                                             │
│   ✓ tkn CLI installed (v0.35.0)                                     │
│   ✓ kubectl connected to cluster (my-cluster)                       │
│   ✓ Namespace 'mert' exists                                         │
│   ✓ hf-secret found                                                 │
│   ✓ s3-secret found                                                 │
│   ✓ model-pvc bound (300Gi)                                         │
│   ✓ data-pvc bound (20Gi)                                           │
│   ✓ GPU availability: 8 H100 GPUs available (need 4 for TP=4)       │
│   ✓ Experiment ID 'feb04-llama70b-chatsweep-tp4' is unique          │
│                                                                     │
│ [2/7] Applying Tekton task definitions                              │
│   ✓ Applied 2 StepActions                                           │
│   ✓ Applied 23 Tasks                                                │
│                                                                     │
│ [3/7] Generating configuration files                                │
│   → /tmp/blis-feb04-llama70b-chatsweep-tp4/values.yaml             │
│   → /tmp/blis-feb04-llama70b-chatsweep-tp4/pipelinerun.yaml        │
│                                                                     │
│ [4/7] Building pipeline from template                               │
│   ✓ tektonc compiled successfully                                   │
│   → /tmp/blis-feb04-llama70b-chatsweep-tp4/pipeline.yaml           │
│                                                                     │
│ [5/7] Deploying to cluster                                          │
│   ✓ Pipeline 'blis-test' applied                                    │
│   ✓ PipelineRun 'feb04-llama70b-chatsweep-tp4' created              │
│                                                                     │
│ [6/7] Monitoring deployment...                                      │
│                                                                     │
│   TaskRun Status:                                                   │
│   ┌────────────────────────────┬───────────┬──────────┐             │
│   │ Task                       │ Status    │ Duration │             │
│   ├────────────────────────────┼───────────┼──────────┤             │
│   │ download-model             │ Running   │ 2m 15s   │             │
│   │ install-guidellm           │ Pending   │ -        │             │
│   │ create-exp-config-4        │ Pending   │ -        │             │
│   │ deploy-otel-collector-4    │ Pending   │ -        │             │
│   │ deploy-model-4             │ Pending   │ -        │             │
│   │ run-workload-4             │ Pending   │ -        │             │
│   │ delete-model-4             │ Pending   │ -        │             │
│   │ raw-upload-4               │ Pending   │ -        │             │
│   └────────────────────────────┴───────────┴──────────┘             │
│                                                                     │
│ Commands available:                                                 │
│   • status  - Refresh status                                        │
│   • logs    - View task logs                                        │
│   • pods    - Check vLLM pod status                                 │
│   • cancel  - Cancel pipeline                                       │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 6.2 Model Disambiguation Flow

When a user specifies an ambiguous model name:

```
User: /blis-data-collector run chatbot benchmark with llama-70b

Claude: I noticed "llama-70b" could refer to multiple models:

┌─────────────────────────────────────────────────────────────────────┐
│ WHICH LLAMA 70B MODEL?                                              │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│ [1] Llama 2 70B (meta-llama/Llama-2-70b-hf)                        │
│     • Original Llama 2, 4K context                                  │
│     • Size: 140GB, Download: 30-60 min                              │
│     • Recommended TP: 4-8                                           │
│                                                                     │
│ [2] Llama 3.3 70B (meta-llama/Llama-3.3-70B-Instruct) ← Recommended │
│     • Latest Llama 3.3, 128K context                                │
│     • Size: 140GB, Download: 30-60 min                              │
│     • Improved performance over Llama 2                             │
│     • Recommended TP: 4-8                                           │
│                                                                     │
│ Enter [1] or [2], or type the full HuggingFace model ID:           │
└─────────────────────────────────────────────────────────────────────┘

User: 2

Claude: Using Llama 3.3 70B (meta-llama/Llama-3.3-70B-Instruct).
[Continues to next step...]
```

**Tip**: To avoid disambiguation, use explicit names like `llama2-70b` or `llama3-70b`.

### 6.3 Abbreviated Flow for Simple Requests

When user request matches existing config:

```
User: /blis-data-collector run chatsweep with llama-7b in namespace mert

Claude: Your request matches current values.yaml. Only pipelinerun.yaml
params need to be set.

╔═══════════════════════════════════════════════════════════════════╗
║  Experiment: feb04-llama7b-chatsweep                              ║
║  Model: meta-llama/Llama-2-7b-hf                                  ║
║  Namespace: mert                                                  ║
║  Workload: chatsweep (already configured)                         ║
║  TP: [2, 4] (using values.yaml default)                           ║
║  vLLM: vllm/vllm-openai:v0.11.0 (default)                        ║
╚═══════════════════════════════════════════════════════════════════╝

No configuration changes needed. Proceed? [Y/n]
```

---

## Configuration Files

### 7.1 File Structure

```
.claude/
├── skills/
│   └── blis-data-collector.md       # Main skill definition
└── config/
    ├── known-models.yaml            # Model aliases and recommended settings
    ├── workload-presets.yaml        # Workload name mappings
    └── vllm-args.yaml               # Known vLLM arguments reference
```

### 7.2 known-models.yaml

```yaml
# Model aliases for natural language matching
# IMPORTANT: Ambiguous aliases require disambiguation (see ambiguous_aliases below)
aliases:
  # Llama 2 family (explicit)
  "llama2-7b": "meta-llama/Llama-2-7b-hf"
  "llama-2-7b": "meta-llama/Llama-2-7b-hf"
  "llama2-13b": "meta-llama/Llama-2-13b-hf"
  "llama-2-13b": "meta-llama/Llama-2-13b-hf"
  "llama2-70b": "meta-llama/Llama-2-70b-hf"
  "llama-2-70b": "meta-llama/Llama-2-70b-hf"

  # Llama 3 family (explicit)
  "llama3-8b": "meta-llama/Llama-3-8B-Instruct"
  "llama-3-8b": "meta-llama/Llama-3-8B-Instruct"
  "llama3-70b": "meta-llama/Llama-3.3-70B-Instruct"
  "llama-3-70b": "meta-llama/Llama-3.3-70B-Instruct"
  "llama-3.3-70b": "meta-llama/Llama-3.3-70B-Instruct"

  # CodeLlama family
  "codellama-34b": "codellama/CodeLlama-34b-Instruct-hf"
  "codellama-7b": "codellama/CodeLlama-7b-Instruct-hf"

  # Qwen family
  "qwen-1.5b": "Qwen/Qwen2.5-Math-1.5B"
  "qwen-7b": "Qwen/Qwen2.5-7B-Instruct"

  # Mistral family
  "mistral-7b": "mistralai/Mistral-7B-Instruct-v0.3"

# Ambiguous aliases that REQUIRE user disambiguation
# When user says these, the skill MUST ask which version they mean
ambiguous_aliases:
  "llama-7b":
    options:
      - id: "meta-llama/Llama-2-7b-hf"
        label: "Llama 2 7B"
        description: "Original Llama 2, well-tested, 4K context"
      - id: "meta-llama/Llama-3-8B-Instruct"
        label: "Llama 3 8B"
        description: "Newer Llama 3, improved performance, 8K context"
    prompt: "Did you mean Llama 2 (7B) or Llama 3 (8B)?"

  "llama-70b":
    options:
      - id: "meta-llama/Llama-2-70b-hf"
        label: "Llama 2 70B"
        description: "Original Llama 2, 4K context"
      - id: "meta-llama/Llama-3.3-70B-Instruct"
        label: "Llama 3.3 70B"
        description: "Latest Llama 3.3, 128K context, improved performance"
    prompt: "Did you mean Llama 2 (70B) or Llama 3.3 (70B)?"

  "llama":
    options:
      - id: "meta-llama/Llama-2-7b-hf"
        label: "Llama 2 7B"
      - id: "meta-llama/Llama-3-8B-Instruct"
        label: "Llama 3 8B"
      - id: "meta-llama/Llama-3.3-70B-Instruct"
        label: "Llama 3.3 70B"
    prompt: "Which Llama model do you want to use?"

# Recommended settings per model
recommended_settings:
  "meta-llama/Llama-2-7b-hf":
    tp: [1, 2]
    max_model_len: 4096
    max_num_seqs: 256
    model_size_gb: 14
    estimated_download_time: "5-10 minutes"
    notes: "Standard 7B model, works well with TP=1-2 on H100"

  "meta-llama/Llama-2-70b-hf":
    tp: [4, 8]
    max_model_len: 4096
    max_num_seqs: 128
    model_size_gb: 140
    estimated_download_time: "30-60 minutes"
    notes: "Large Llama 2 model, requires TP=4+ on H100"

  "meta-llama/Llama-3.3-70B-Instruct":
    tp: [4, 8]
    max_model_len: 8192
    max_num_seqs: 128
    model_size_gb: 140
    estimated_download_time: "30-60 minutes"
    notes: "Large model, requires TP=4+ on H100"
    requires_trust_remote_code: false

  "codellama/CodeLlama-34b-Instruct-hf":
    tp: [2, 4]
    max_model_len: 4096
    max_num_seqs: 128
    model_size_gb: 68
    estimated_download_time: "15-25 minutes"
    notes: "Code completion model"

# Custom image recommendations
custom_images:
  journey_tracing:
    image: "ghcr.io/ibm/vllm-journey"
    tags: ["v0.8.0", "v0.9.0", "latest"]
    required_args:
      - "--journey-tracing-sample-rate"
    notes: "Custom vLLM with journey tracing support"
```

### 7.3 workload-presets.yaml

```yaml
# Natural language to preset mapping
mappings:
  chatsweep:
    keywords: ["chatbot", "chat", "conversation", "conversational", "dialogue"]
    description: "Short prompts with medium outputs, includes prefix caching"

  codesweep:
    keywords: ["code", "coding", "programming", "completion", "autocomplete"]
    description: "Long prompts (code context) with short outputs (completions)"

  train:
    keywords: ["training", "fine-tuning", "learning"]
    description: "Variable length inputs and outputs for training scenarios"

  summarization:
    keywords: ["summary", "summarization", "summarize", "tldr", "condense"]
    description: "Long input documents with medium-length summaries"

  prefilldominant:
    keywords: ["prefill", "prefill-heavy", "long-context", "context-heavy"]
    description: "Very long prompts with minimal outputs"

  chatbot:
    keywords: ["basic-chat", "simple-chat"]
    description: "Balanced prompts and outputs for general chat"

  contentgen:
    keywords: ["content", "generation", "writing", "creative", "blog"]
    description: "Medium prompts with long generated outputs"

  doc:
    keywords: ["document", "long-form", "documentation", "report"]
    description: "Very long prompts and outputs for document processing"
```

### 7.4 vllm-args.yaml

```yaml
# vLLM argument reference for the skill
categories:
  # Core performance arguments (commonly changed)
  performance:
    - name: "--max-model-len"
      type: integer
      description: "Maximum sequence length the model can handle"
      values_yaml_path: "stack.MAX_MODEL_LEN"
      common_values: [2048, 4096, 8192, 16384, 32768]

    - name: "--max-num-batched-tokens"
      type: integer
      description: "Maximum number of tokens in a batch"
      values_yaml_path: "stack.MAX_NUM_BATCHED_TOKENS"
      common_values: [1024, 2048, 4096, 8192]

    - name: "--max-num-seqs"
      type: integer
      description: "Maximum number of concurrent sequences"
      values_yaml_path: "stack.MAX_NUM_SEQS"
      common_values: [32, 64, 128, 256, 512]

    - name: "--gpu-memory-utilization"
      type: float
      description: "Fraction of GPU memory to use"
      default: 0.9
      range: [0.1, 0.99]

  # Tracing and observability
  tracing:
    - name: "--otlp-traces-endpoint"
      type: string
      description: "OTLP endpoint for trace export"
      auto_configured: true
      notes: "Automatically set based on OTEL collector deployment"

    - name: "--journey-tracing-sample-rate"
      type: float
      description: "Sample rate for journey tracing"
      range: [0.0, 1.0]
      requires_custom_image: true
      custom_image_notes: "Only available in custom vLLM builds with journey tracing"

  # Model loading
  model_loading:
    - name: "--trust-remote-code"
      type: flag
      description: "Allow execution of custom model code from HuggingFace"
      security_note: "Only enable for trusted models"

    - name: "--dtype"
      type: enum
      values: ["auto", "half", "float16", "bfloat16", "float", "float32"]
      description: "Data type for model weights"
      default: "auto"

    - name: "--quantization"
      type: enum
      values: ["awq", "gptq", "squeezellm", "fp8", null]
      description: "Quantization method for the model"
      notes: "Requires model to be pre-quantized in matching format"

    - name: "--load-format"
      type: enum
      values: ["auto", "pt", "safetensors", "npcache", "dummy"]
      description: "Format to load model weights"

  # Optimization flags
  optimization:
    - name: "--enable-prefix-caching"
      type: flag
      description: "Enable automatic prefix caching"
      notes: "Useful for workloads with repeated prefixes"

    - name: "--enforce-eager"
      type: flag
      description: "Disable CUDA graph optimization"
      notes: "May help with debugging or memory issues"

    - name: "--enable-chunked-prefill"
      type: flag
      description: "Enable chunked prefill optimization"

    - name: "--disable-log-stats"
      type: flag
      description: "Disable logging of statistics"

  # Speculative decoding
  speculative:
    - name: "--speculative-model"
      type: string
      description: "Draft model for speculative decoding"

    - name: "--num-speculative-tokens"
      type: integer
      description: "Number of tokens to speculatively decode"
      common_values: [3, 5, 7]
```

### 7.5 blis-data-collector.md (Skill Definition)

```markdown
---
name: blis-data-collector
description: |
  Automates BLIS LLM data collection pipeline on Tekton.
  Handles cluster validation, pipeline deployment, monitoring, and data retrieval.
  Supports custom vLLM images and arguments.
triggers:
  - /blis-data-collector
  - /blis
allowed_tools:
  - Bash
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - AskUserQuestion
---

# BLIS Data Collector Skill

## Overview

You are the BLIS Data Collector, an automation assistant for running LLM benchmarking
experiments on Tekton pipelines. Your primary goals are:

1. Parse natural language requests to extract experiment parameters
2. Match user requirements to existing `values.yaml` configurations
3. Explicitly verify vLLM configuration before deployment
4. Minimize file modifications by using temporary overrides
5. Guide users through the complete workflow

## Parameter Extraction

When a user invokes this skill, extract the following parameters:

### Required (ask if missing):
- **model**: The HuggingFace model ID (use known-models.yaml for aliases)
- **namespace**: Kubernetes namespace for deployment

### Optional (use defaults from values.yaml):
- **experimentId**: Auto-generate as `{date}-{model-short}-{workload}` if not specified
- **tensorParallelism**: Default from `stack.treatments.tensorParallelism`
- **workload**: Default from `stack.workload.app`
- **MAX_MODEL_LEN**: Default from `stack.MAX_MODEL_LEN`
- **MAX_NUM_BATCHED_TOKENS**: Default from `stack.MAX_NUM_BATCHED_TOKENS`
- **MAX_NUM_SEQS**: Default from `stack.MAX_NUM_SEQS`

### vLLM Configuration (verify explicitly):
- **vllm.image**: Default `vllm/vllm-openai:v0.11.0`
- **vllm.additionalArgs**: Additional CLI arguments (e.g., `--trust-remote-code`)

## Workflow Steps

### Step 1: Parse User Request
Extract all parameters from the natural language request. Use fuzzy matching for:
- Model names (e.g., "llama-70b" → "meta-llama/Llama-3.3-70B-Instruct")
- Workload types (e.g., "chatbot" → "chatsweep")

### Step 2: Load and Compare with values.yaml
Read `tektoncsample/blis/values.yaml` and compare user requirements:
- Identify MATCHES (no change needed)
- Identify SUBSETS (filter arrays)
- Identify OVERRIDES (need temp file)

### Step 3: Explicit vLLM Verification
ALWAYS ask the user to verify vLLM configuration if:
- Custom image is requested
- Additional arguments are specified
- ANY vLLM parameter differs from values.yaml defaults

Display the complete vLLM configuration including:
- Image (main and init containers)
- All arguments (from values.yaml + user additions)
- Environment variables
- Resource limits

Ask: "Do you want to modify any vLLM settings?"

### Step 4: Show Available Workload Presets
If workload is not specified, show available options from `pre-defined-workloads.yaml`:
- chatsweep: Chatbot-style with prefix caching
- codesweep: Code completion workload
- (list others)

### Step 5: Final Confirmation
Display complete configuration summary with:
- Changes from values.yaml highlighted
- vLLM configuration details
- Expected resource usage

### Step 6: Execute Workflow
Run the following commands:

```bash
# 1. Pre-flight checks (ALL must pass before proceeding)
# 1a. CLI tools
which tkn || echo "ERROR: tkn CLI not installed"
tkn version
kubectl config current-context

# 1b. Namespace and resources
kubectl get ns ${NAMESPACE}
kubectl get secret hf-secret s3-secret -n ${NAMESPACE}
kubectl get pvc model-pvc data-pvc -n ${NAMESPACE}

# 1c. GPU availability check (CRITICAL for vLLM)
# Check if enough GPUs are available for requested TP
kubectl get nodes -l nvidia.com/gpu.product=NVIDIA-H100-80GB-HBM3 \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}'
# Verify: Total available GPUs >= requested TP

# 1d. Experiment ID collision check (prevent overwriting)
kubectl get pipelinerun ${EXPERIMENT_ID} -n ${NAMESPACE} 2>/dev/null && \
  echo "ERROR: PipelineRun ${EXPERIMENT_ID} already exists!" && exit 1

# 2. Apply Tekton tasks
for step in tekton/steps/*.yaml; do kubectl apply -f "$step"; done
for task in tekton/tasks/*.yaml; do kubectl apply -f "$task"; done

# 3. Generate temp config files (if overrides needed)
# Write temp values.yaml with overrides
# Write temp pipelinerun.yaml with params

# 4. Build pipeline
python tektonc/tektonc.py \
  -t tektoncsample/blis/data_pipeline.yaml.j2 \
  -f ${TEMP_VALUES_PATH} \
  -r ${TEMP_PIPELINERUN_PATH} \
  -o ${TEMP_PIPELINE_PATH}

# 5. Deploy
kubectl apply -f ${TEMP_PIPELINE_PATH}
kubectl apply -f ${TEMP_PIPELINERUN_PATH}

# 6. Monitor (see Monitoring Behavior section below)
tkn pr describe ${EXPERIMENT_ID} -n ${NAMESPACE}
```

### Step 7: Monitoring Behavior

**Polling Configuration:**
- **Polling interval**: Every 30 seconds
- **vLLM pod check interval**: Every 60 seconds (separate from TaskRun status)
- **Timeout warning**: Alert user if any task exceeds expected duration by 2x

**Completion Detection:**
A pipeline is considered **complete** when:
- All TaskRuns reach `Succeeded` status → **SUCCESS**
- Any TaskRun reaches `Failed` status → **FAILURE** (stop monitoring, offer recovery)
- PipelineRun reaches `Cancelled` status → **CANCELLED**

**Monitoring Commands:**
```bash
# Refresh TaskRun status
tkn tr list -n ${NAMESPACE} | grep ${EXPERIMENT_ID}

# Get detailed PipelineRun status
tkn pr describe ${EXPERIMENT_ID} -n ${NAMESPACE}

# Check vLLM pod status (critical for deploy-model tasks)
kubectl get pods -n ${NAMESPACE} -l llm-d.ai/model=${MODEL_LABEL} \
  -o custom-columns=NAME:.metadata.name,STATUS:.status.phase,READY:.status.containerStatuses[0].ready,RESTARTS:.status.containerStatuses[0].restartCount

# Get vLLM logs if pod is unhealthy
kubectl logs -n ${NAMESPACE} -l llm-d.ai/model=${MODEL_LABEL} --tail=50
```

**Status Display Format:**
```
Pipeline: feb04-llama70b-chatsweep-tp4
Status: Running (elapsed: 15m 32s)
Last updated: 2026-02-04 10:45:32 (refreshes every 30s)

TaskRuns:
┌────────────────────────────┬───────────┬──────────┬─────────────┐
│ Task                       │ Status    │ Duration │ Expected    │
├────────────────────────────┼───────────┼──────────┼─────────────┤
│ download-model             │ Succeeded │ 8m 15s   │ 5-30m       │
│ install-guidellm           │ Succeeded │ 45s      │ 1-2m        │
│ deploy-model-4             │ Running   │ 5m 32s   │ 5-15m       │
│ run-workload-4             │ Pending   │ -        │ 30-120m     │
└────────────────────────────┴───────────┴──────────┴─────────────┘

vLLM Pod Status:
  Name: llama70b-exp-4-decode-0
  Status: Running
  Ready: true
  GPU Memory: 4x H100 (TP=4)
```

### Step 8: Post-Run
After completion, offer:
- Data retrieval from PVC
- Cleanup of resources

## Error Handling

### vLLM Deployment Failures
If vLLM pod fails, check:
```bash
kubectl describe pod -l llm-d.ai/model=${LABEL} -n ${NAMESPACE}
kubectl logs -l llm-d.ai/model=${LABEL} -n ${NAMESPACE} --tail=100
```

Common issues:
- OOM: Suggest increasing TP or reducing max_model_len
- Image pull: Verify image name and registry access
- GPU unavailable: Check node availability

### TaskRun Failures
```bash
tkn tr describe ${TASKRUN_NAME} -n ${NAMESPACE}
tkn tr logs ${TASKRUN_NAME} -n ${NAMESPACE}
```

## Important Notes

- NEVER modify the original `values.yaml` or `pipelinerun.yaml` files
- ALWAYS use temporary files in `/tmp/blis-${EXPERIMENT_ID}/`
- ALWAYS verify vLLM configuration explicitly with the user
- If in doubt about a parameter, ASK the user
```

---

## Implementation Details

### 8.1 Temporary File Generation

When overrides are needed, generate files in `/tmp/blis-{experimentId}/`:

```
/tmp/blis-feb04-llama70b-chatsweep-tp4/
├── values.yaml          # Merged with user overrides
├── pipelinerun.yaml     # With experiment params
├── pipeline.yaml        # Output from tektonc
└── state.json           # Tracking file
```

### 8.2 Values Override Generation

```yaml
# Example: temp values.yaml with vLLM image and args override
# Generated when user requests custom image + args

# Start with base from tektoncsample/blis/values.yaml, then override:

stack:
  treatments:
    tensorParallelism:
      - 4  # User filtered from [2, 4] to [4]

  # These stay as defaults since user didn't change them
  MAX_MODEL_LEN: 4096
  MAX_NUM_BATCHED_TOKENS: 2048
  MAX_NUM_SEQS: 256

  model:
    helmValues:
      # ... base config ...
      decode:
        containers:
        - name: "vllm"
          image: "ghcr.io/ibm/vllm-journey:v0.8.0"  # OVERRIDE
          args:
            - "--trust-remote-code"                  # ADDED
            - "--journey-tracing-sample-rate"        # ADDED
            - "1.0"                                  # ADDED
          # ... rest of container config ...
        initContainers:
        - name: install-otel-dependencies
          image: "ghcr.io/ibm/vllm-journey:v0.8.0"  # OVERRIDE (match main)
          # ... rest of init config ...
```

### 8.3 Pipeline Override Injection

For vLLM arguments that are injected via pipeline overrides (in `data_pipeline.yaml.j2`), extend the overrides list:

```yaml
# In generated pipelinerun or through values that flow to pipeline
# Additional overrides for custom args:

overrides:
  # Standard overrides from template
  - decode.parallelism.tensor={{ tp }}
  - decode.containers[name="vllm"].args=--max-model-len={{ stack.MAX_MODEL_LEN }}
  - decode.containers[name="vllm"].args=--max-num-batched-tokens={{ stack.MAX_NUM_BATCHED_TOKENS }}
  - decode.containers[name="vllm"].args=--max-num-seqs={{ stack.MAX_NUM_SEQS }}

  # User-specified additional args
  - decode.containers[name="vllm"].args=--journey-tracing-sample-rate=1.0
  - decode.containers[name="vllm"].args=--trust-remote-code

  # Image override
  - decode.containers[name="vllm"].image=ghcr.io/ibm/vllm-journey:v0.8.0
  - decode.initContainers[0].image=ghcr.io/ibm/vllm-journey:v0.8.0
```

---

## Pre-flight Checks (Detailed)

All pre-flight checks must pass before deployment proceeds. If any check fails, the skill should report the failure and offer remediation.

### 9.0 Pre-flight Check Matrix

| Check | Command | Pass Criteria | Failure Action |
|-------|---------|---------------|----------------|
| **tkn CLI** | `tkn version` | Returns version string | Prompt user to install: `brew install tektoncd-cli` |
| **kubectl** | `kubectl cluster-info` | Connects successfully | Check kubeconfig |
| **Namespace** | `kubectl get ns ${NS}` | Namespace exists | Offer to create it |
| **hf-secret** | `kubectl get secret hf-secret -n ${NS}` | Secret exists | Show creation command |
| **s3-secret** | `kubectl get secret s3-secret -n ${NS}` | Secret exists | Show creation command |
| **model-pvc** | `kubectl get pvc model-pvc -n ${NS}` | PVC bound, ≥300Gi | Show creation YAML |
| **data-pvc** | `kubectl get pvc data-pvc -n ${NS}` | PVC bound, ≥20Gi | Show creation YAML |
| **GPU availability** | See below | Available GPUs ≥ TP | Warn user, suggest lower TP |
| **Experiment ID** | `kubectl get pr ${ID} -n ${NS}` | Returns "not found" | Suggest unique ID |

### GPU Availability Check

```bash
# Get allocatable GPUs per node
kubectl get nodes -l nvidia.com/gpu.product=NVIDIA-H100-80GB-HBM3 \
  -o jsonpath='{range .items[*]}{.metadata.name}: {.status.allocatable.nvidia\.com/gpu} GPUs{"\n"}{end}'

# Get currently used GPUs
kubectl get pods --all-namespaces -o json | \
  jq '[.items[].spec.containers[].resources.limits."nvidia.com/gpu" // 0 | tonumber] | add'
```

**Decision logic:**
- If `available_gpus >= requested_TP`: ✓ Pass
- If `available_gpus < requested_TP`: ⚠ Warn user
  - "Only {N} GPUs available, but TP={TP} requires {TP} GPUs"
  - "Options: [1] Wait for GPUs [2] Reduce TP to {available}"

### Experiment ID Collision Check

```bash
# Check if PipelineRun already exists
kubectl get pipelinerun ${EXPERIMENT_ID} -n ${NAMESPACE} 2>/dev/null

# If exists (exit code 0): COLLISION
# If not exists (exit code 1): OK
```

**If collision detected:**
```
⚠ Experiment ID 'feb04-llama70b-chatsweep-tp4' already exists!

Options:
  [1] Use unique ID: feb04-llama70b-chatsweep-tp4-v2
  [2] Delete existing PipelineRun and reuse ID
  [3] Enter custom experiment ID
```

---

## Error Handling

### 9.1 Common vLLM Errors and Remediation

| Error | Detection | Suggested Fix |
|-------|-----------|---------------|
| CUDA OOM | `RuntimeError: CUDA out of memory` | Increase TP, reduce max_model_len, reduce max_num_seqs |
| Image Pull Error | `ImagePullBackOff` | Verify image name, check registry credentials |
| Model Not Found | `OSError: [model] does not exist` | Verify model path, check HF token |
| Trust Remote Code | `ValueError: trust_remote_code` | Add `--trust-remote-code` flag |
| Quantization Mismatch | `quantization config not found` | Verify model format matches `--quantization` flag |
| GPU Not Schedulable | Pod stuck in `Pending` | Check GPU availability, node selector |

### 9.2 Recovery Actions

```
┌─────────────────────────────────────────────────────────────────────┐
│ ERROR RECOVERY OPTIONS                                              │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│ vLLM OOM Error Detected                                            │
│                                                                     │
│ Current config:                                                     │
│   TP=2, max_model_len=8192, max_num_seqs=256                       │
│                                                                     │
│ Suggested fixes:                                                    │
│   1. [Increase TP] TP=2 → TP=4 (doubles GPU memory)               │
│   2. [Reduce context] max_model_len=8192 → 4096                    │
│   3. [Reduce batch] max_num_seqs=256 → 128                         │
│   4. [Both 2+3] Reduce context and batch size                      │
│                                                                     │
│ Select option [1-4] or describe custom fix:                        │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Quick Reference

### Invoking the Skill

```bash
# Full specification
/blis-data-collector run llama-70b chatbot workload with TP=4 in namespace mert

# Minimal (will prompt for missing info)
/blis-data-collector llama-7b chatbot

# With custom vLLM
/blis-data-collector llama-7b with custom image ghcr.io/myorg/vllm:v1 and --journey-tracing-sample-rate 1.0

# With vLLM args
/blis-data-collector llama-7b --trust-remote-code --enable-prefix-caching
```

### Available Workload Presets

| Preset | Use Case | Prompt Length | Output Length |
|--------|----------|---------------|---------------|
| `chatsweep` | Chatbot | Short (70) | Medium (215) |
| `codesweep` | Code completion | Long (2048) | Short (28) |
| `train` | Training data | Variable (1700) | Variable (800) |
| `summarization` | Summarization | Very long (4096) | Medium (512) |
| `prefilldominant` | Long context | Very long (2048) | Very short (32) |
| `chatbot` | Basic chat | Medium (256) | Medium (256) |
| `contentgen` | Content gen | Medium (1024) | Long (1024) |
| `doc` | Document | Very long (9000) | Long (1536) |

### Common vLLM Arguments

| Argument | Purpose |
|----------|---------|
| `--trust-remote-code` | Allow custom model code |
| `--enable-prefix-caching` | Cache repeated prefixes |
| `--enforce-eager` | Disable CUDA graphs |
| `--gpu-memory-utilization 0.9` | GPU memory fraction |
| `--dtype bfloat16` | Model precision |
| `--quantization awq` | Use quantized model |
| `--journey-tracing-sample-rate 1.0` | Journey tracing (custom image) |
