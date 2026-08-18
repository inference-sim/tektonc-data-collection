# vLLM Log Patterns Reference

Quick reference for extracting vLLM config from logs.

## Example Log Lines (from experiment #73 — note: pre-v0.26 legacy example)

> **⚠️ Legacy example.** This #73 log predates the v0.26 offload change. The `kv_offloading_size: 8.0`
> and `disable_hybrid_kv_cache_manager: True` flags below are **deprecated** — current runs on
> `vllm/vllm-openai:v0.26.0` use `kv_transfer_config` (OffloadingConnector) instead. See the
> "KV Offload (v0.26 OffloadingConnector)" section below for the current pattern. The non-offload
> fields (tp, mbt, max_model_len) are still valid to extract.

### Non-default Args (line ~2-10)
```
2026-04-21 15:53:30,020 INFO vllm.entrypoints.utils: non-default args: {'model_tag': '/model-cache/models/codellama/CodeLlama-34b-Instruct-hf', 'api_server_count': 1, 'model': '/model-cache/models/codellama/CodeLlama-34b-Instruct-hf', 'max_model_len': 4096, 'served_model_name': ['codellama/CodeLlama-34b-Instruct-hf'], 'tensor_parallel_size': 2, 'block_size': 16, 'kv_offloading_size': 8.0, 'max_num_batched_tokens': 2048, 'max_num_seqs': 128, 'disable_hybrid_kv_cache_manager': True}
```

**Extract:**
- `tensor_parallel_size`: 2
- `max_num_batched_tokens`: 2048
- `max_model_len`: 4096
- ~~`kv_offloading_size`: 8.0~~ (DEPRECATED pre-v0.26 offload flag — current runs use `kv_transfer_config`, see below)

### Model Path (line ~1)
```
2026-04-21 15:53:30,017 INFO vllm.entrypoints.openai.api_server: vLLM server version 0.15.1, serving model /model-cache/models/codellama/CodeLlama-34b-Instruct-hf
```

**Extract:** `codellama/CodeLlama-34b-Instruct-hf` (strip `/model-cache/models/` prefix)

### Max Model Len Confirmation (line ~15)
```
2026-04-21 15:53:34,874 INFO vllm.config.model: Using max model len 4096
```

**Extract:** 4096

### Chunked Prefill (line ~20)
```
2026-04-21 15:53:35,175 INFO vllm.config.scheduler: Chunked prefill is enabled with max_num_batched_tokens=2048.
```

**Extract:** Chunked prefill = enabled, confirms mbt=2048

### Engine Config (line ~25-30, long line)
```
2026-04-21 15:53:40,132 INFO vllm.v1.engine.core: Initializing a V1 LLM engine (v0.15.1) with config: model='/model-cache/models/codellama/CodeLlama-34b-Instruct-hf', ... tensor_parallel_size=2, ... max_seq_len=4096, ... enable_prefix_caching=True, enable_chunked_prefill=True, ...
```

**Extract:**
- `enable_prefix_caching`: True
- `enable_chunked_prefill`: True
- Confirms TP, model len again

### Scheduling Policy (if priority: true in experiments.json)

**In non-default args:**
```
2026-04-30 12:34:56,789 INFO vllm.entrypoints.utils: non-default args: {'model': '...', 'tensor_parallel_size': 2, 'scheduling_policy': 'priority', ...}
```

**Or in engine config:**
```
2026-04-30 12:34:58,012 INFO vllm.config.scheduler: Using scheduler policy: priority
```

**Extract:** `scheduling_policy` = "priority"

### vLLM Version

```
2026-04-30 12:34:55,123 INFO vllm.entrypoints.openai.api_server: vLLM server version 0.17.1, serving model /model-cache/models/...
```

**Extract:** Version 0.17.1

### KV Offload (v0.26 OffloadingConnector) — when `kv_offload: true`

**In non-default args** (config presence):
```
2026-08-18 14:42:11,382 INFO vllm.entrypoints.serve.utils.api_utils: non-default args: {..., 'kv_transfer_config': KVTransferConfig(kv_connector='OffloadingConnector', ..., kv_role='kv_both', ..., kv_connector_extra_config={'spec_name': 'CPUOffloadingSpec', 'cpu_bytes_to_use': 10737418240, 'block_size': 16, 'eviction_policy': 'lru'}, ...)}
```

**Init (pool created):**
```
2026-08-18 14:43:11,914 INFO vllm.v1.kv_offload.factory: Creating offloading spec with name: CPUOffloadingSpec
2026-08-18 14:43:11,928 INFO vllm.v1.kv_offload.cpu.gpu_worker: Allocating 40 CPU tensors...
```

**Runtime transfer metrics (logged periodically during observe):**
```
2026-08-18 14:44:23,399 INFO vllm.v1.metrics.loggers: KV Transfer metrics: vllm:kv_offload_store_bytes=1381498880, vllm:kv_offload_store_size_count=19, vllm:kv_offload_cpu_cache_usage_perc=0.0058, vllm:kv_offload_cpu_cache_read_usage_perc=0.0, ...
```

**Extract:**
- `kv_connector`: OffloadingConnector; `spec_name`: CPUOffloadingSpec (confirms v0.26 offload configured)
- `kv_offload_store_bytes`: GPU→CPU bytes offloaded (> 0 = offload triggered)
- `kv_offload_cpu_cache_read_usage_perc`: CPU→GPU restore activity (0 = eviction-only, no cache-hit restores)

**Deprecated (v0.19 and earlier — should NOT appear on v0.26 images):** `kv_offloading_size=8.0`, `disable_hybrid_kv_cache_manager: True`. If present, the run used an old binary.

## Grep Commands

```bash
# Get all config in one line
grep "non-default args:" vllm.log | head -1

# Check specific params
grep "serving model" vllm.log | head -1
grep "tensor_parallel_size" vllm.log | head -1
grep "max_num_batched_tokens" vllm.log | head -1
grep "Using max model len" vllm.log | head -1
grep "enable_prefix_caching" vllm.log | head -1
grep "kv_transfer_config" vllm.log | head -1              # v0.26 CPU offload config (OffloadingConnector)
grep "CPUOffloadingSpec" vllm.log | head -2              # offload pool init
grep "KV Transfer metrics" vllm.log | tail -1           # runtime offload activity (store_bytes, read_usage)
grep "Chunked prefill" vllm.log | head -1
grep "scheduling_policy" vllm.log | head -1
grep "vLLM server version" vllm.log | head -1
grep -i "priority" vllm.log | head -5  # Check for priority in request bodies
```

## Mapping to experiments.json

| experiments.json field | vllm.log field | Example |
|------------------------|----------------|---------|
| `model` | `model` path (strip prefix) | codellama/CodeLlama-34b-Instruct-hf |
| `tp` | `tensor_parallel_size` | 2 |
| `mbt` | `max_num_batched_tokens` | 2048 |
| `max_model_len` | `max_seq_len` or `max_model_len` | 4096 |
| `kv_offload: true` | `kv_transfer_config` (OffloadingConnector, CPUOffloadingSpec) | cpu_bytes_to_use=10737418240 |
| ~~`cpu_offload: true`~~ | ~~`kv_offloading_size` = 8.0~~ (DEPRECATED, pre-v0.26) | — |
| (workload expectation) | `enable_prefix_caching` | True |
| `priority: true` | `scheduling_policy` = "priority" | "priority" |
| `vllm_version` | "vLLM server version X.Y.Z" | 0.17.1 |

## What to Check

1. **Model name match**: Strip path prefix, compare base model name
2. **TP degree**: Exact match required
3. **MBT**: Exact match required (common mismatch if default changed)
4. **Max model len**: Should match if specified in experiments.json
5. **KV offload (v0.26)**: If `kv_offload: true`, must see `kv_transfer_config` with `OffloadingConnector`/`CPUOffloadingSpec` in non-default args, plus `Creating offloading spec` + `Allocating N CPU tensors` at init. For runtime activity, check `KV Transfer metrics` lines (`store_bytes` > 0 = offload triggered).
6. **Prefix caching**: Should be True if workload uses shared prefixes
7. **Scheduling policy**: If `priority: true`, must see `scheduling_policy="priority"` or `policy='priority'` in logs
8. **vLLM version**: If `vllm_version` specified, must match version in "vLLM server version X.Y.Z" line
9. **Priority field in requests**: If `priority: true`, check observe/data.csv for `priority` column presence

## Common Issues

- **Model name mismatch**: Path might have `/model-cache/models/` prefix or different HF org
- **MBT not in non-default args**: Means it used vLLM default (check release notes)
- **KV offload config (v0.26)**: When `kv_offload: true`, `kv_transfer_config` with `OffloadingConnector`/`CPUOffloadingSpec` (cpu_bytes_to_use=10737418240) must appear; set by blis-campaign/generate.py from the `kv_offload` flag. The old `kv_offloading_size=8.0` flag is deprecated and won't appear on v0.26 images.
- **Offload configured but idle**: `kv_transfer_config` present but `KV Transfer metrics` show `store_bytes=0` all run → workload never pressured the GPU cache; offload had no runtime effect (WARN, not FAIL).
- **Multiple restarts**: vLLM may restart; use `head -1` to get first successful startup
