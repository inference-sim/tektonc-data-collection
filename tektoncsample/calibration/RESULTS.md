# H100 Rate Calibration Results

**Date:** 2026-03-12
**Cluster:** pokprod001 (14 nodes x 8 H100-80GB-HBM3)
**Namespace:** diya
**vLLM version:** vllm/vllm-openai:v0.15.1
**Pipeline runs:** cal-h100-d55vl, cal-h100-kmdzs

## Method

Rate calibration measures `decode_ms_per_token` for each unique (model, hardware, TP)
configuration. This value is used to compute `safe_rps` — the maximum request rate that
avoids queue saturation during the full experiment campaign.

### Procedure

1. Deploy the model via the `llm-d-modelservice` Helm chart with experiment-matching config
   (same vLLM image, same `--max-model-len`, `--max-num-batched-tokens`, `--max-num-seqs`,
   same quantization and model-specific flags)
2. Send one streaming `/v1/completions` request (~500 input tokens, 250 max output tokens,
   `temperature=0`, `stream=true`)
3. Record wall-clock timestamps for each SSE token event
4. Skip the first token (includes TTFT/prefill latency)
5. Compute `decode_ms_per_token = (t_last - t_second) / (n - 2) * 1000` over the remaining
   decode tokens

### Why this works

For sequences under 4K tokens, decode latency is dominated by model weight loading
(GPU memory bandwidth), not KV-cache attention. This makes `decode_ms_per_token` essentially
independent of workload (input/output token counts) and vLLM knobs like `max_num_batched_tokens`,
`kv_offloading_size`, or `gpu_memory_utilization`. Those parameters affect batch capacity
(how many requests can run concurrently) but not per-token decode speed.

Workload differences are handled analytically in the safe rate formula through
`avg_total_tokens` and `avg_output_tokens`.

### Pipeline

All configs run in parallel (each config chain: download -> deploy -> calibrate -> delete).
A `finally` block ensures cleanup even on failure. Template: `data_pipeline.yaml.j2`,
compiled by tektonc.

### Deployment config

| Parameter | Value |
|-----------|-------|
| `--max-model-len` | 4096 |
| `--max-num-batched-tokens` | 2048 |
| `--max-num-seqs` | 128 |
| vLLM image | `vllm/vllm-openai:v0.15.1` |
| GPU | NVIDIA-H100-80GB-HBM3 |
| Replicas | 1 (single decode pod per config) |

Model-specific flags applied where needed (FP8 quantization, generation config overrides).

---

## Results

### Completed calibrations

| Config | Model | TP | Precision | decode_ms/token | TTFT (ms) | Tokens measured |
|--------|-------|----|-----------|----------------:|----------:|----------------:|
| llama2-7b-h100-tp1 | meta-llama/Llama-2-7b-hf | 1 | FP16 | 5.841 | 4.4 | 249 |
| llama31-8b-h100-tp1 | meta-llama/Llama-3.1-8B-Instruct | 1 | FP16 | 6.257 | 5.1 | 249 |
| llama31-8b-h100-tp2 | meta-llama/Llama-3.1-8B-Instruct | 2 | FP16 | 4.138 | 4.9 | 249 |
| qwen3-14b-h100-tp1 | Qwen/Qwen3-14B | 1 | FP16 | 10.916 | 10.3 | 249 |
| codellama-34b-h100-tp2 | codellama/CodeLlama-34b-Instruct-hf | 2 | FP16 | 13.566 | 15.1 | 249 |
| mixtral-8x7b-h100-tp2 | mistralai/Mixtral-8x7B-v0.1 | 2 | FP16 | 6.606 | 6.8 | 249 |
| mixtral-8x7b-h100-tp4 | mistralai/Mixtral-8x7B-v0.1 | 4 | FP16 | 4.424 | 6.1 | 249 |
| scout-17b-h100-tp2 | RedHatAI/Llama-4-Scout-17B-16E-Instruct-FP8-dynamic | 2 | FP8 | 9.948 | 212.1 | 249 |
| llama2-70b-h100-tp4 | meta-llama/Llama-2-70b-hf | 4 | FP16 | 18.252 | 30.6 | 249 |

### Failed (vLLM v0.15.1 FP8 MoE OOM)

| Config | Model | TP | Error |
|--------|-------|----|-------|
| deepseek-v3-h100-tp8 | deepseek-ai/DeepSeek-V3 | 8 | CUDA OOM during FP8 MoE weight init (76.6 GiB allocated, needs 896 MiB more) |
| scout-17b-h100-tp2 | meta-llama/Llama-4-Scout-17B-16E | 2 | ~~CUDA OOM~~ **Resolved** — switched to `RedHatAI/Llama-4-Scout-17B-16E-Instruct-FP8-dynamic` |

**Root cause:** vLLM v0.15.1's online dynamic FP8 quantization (`--quantization fp8`) loads
BF16 weights from the checkpoint and quantizes to FP8 during model construction. For large
MoE models, the peak memory during this conversion (BF16 source + FP8 target buffers
coexisting) exceeds the 80 GB per H100 GPU, even at TP=8 (DeepSeek-V3) or TP=2 (Scout).

The FP8 model weights themselves fit comfortably (DeepSeek-V3: ~84 GB FP8 / 8 GPUs = ~10.5 GB
per GPU; Scout: ~54.5 GB FP8 / 2 GPUs = ~27 GB per GPU). The issue is transient peak memory
during initialization, not steady-state.

**Resolutions:**
- **Scout:** Switched to pre-quantized checkpoint `RedHatAI/Llama-4-Scout-17B-16E-Instruct-FP8-dynamic` (llm-compressor, dynamic FP8, vLLM compatible). Needs re-calibration with new checkpoint.
- **DeepSeek-V3:** No suitable pre-quantized FP8 checkpoint exists. Needs vLLM upgrade or self-quantization with llm-compressor. **Experiments deprioritized.**

---

## Safe Rate Analysis

### Formula

```
safe_rps = 0.5 * min(R_mbt, R_seq)

where:
  R_mbt = mbt * 1000 / (avg_total_tokens * decode_ms_per_token)
  R_seq = max_num_seqs * 1000 / (avg_output_tokens * decode_ms_per_token)
```

The 0.5 multiplier provides a 50% safety margin to account for variance in token generation
times, scheduling overhead, and prefill/decode interleaving.

`R_mbt` captures the throughput limit from `max_num_batched_tokens`: in steady state with
chunked prefill, total tokens per scheduler step (prefill + decode) cannot exceed `mbt`.
`R_seq` captures the limit from `max_num_seqs`: at most 128 requests can decode concurrently.

### Workload profiles

| Workload | avg_input | avg_output | avg_total | Peak rate (rps) |
|----------|----------:|-----------:|----------:|----------------:|
| general | 547 | 248 | 795 | 20.0 |
| codegen | 566 | 247 | 813 | 10.0 |
| roleplay | 750 | 251 | 1001 | 6.0 |
| reasoning | 1034 | 1448 | 2482 | 4.0 |

### Per-experiment safe rates (H100, calibrated models only)

Default config: mbt=2048, max_num_seqs=128 (unless noted).

| Exp | Model | TP | Workload | decode_ms | safe_rps | peak | Verdict |
|----:|-------|---:|----------|----------:|---------:|-----:|---------|
| 1 | Codellama-34b | 2 | general | 13.566 | 19.0 | 20.0 | **UNSAFE** |
| 2 | Codellama-34b | 2 | codegen | 13.566 | 19.1 | 10.0 | ok |
| 3 | Codellama-34b | 2 | roleplay | 13.566 | 18.8 | 6.0 | ok |
| 4 | Codellama-34b | 2 | reasoning | 13.566 | 3.3 | 4.0 | **UNSAFE** |
| 5 | Llama-2-70b | 4 | general | 18.252 | 14.1 | 20.0 | **UNSAFE** |
| 6 | Llama-2-70b | 4 | codegen | 18.252 | 14.2 | 10.0 | ok |
| 7 | Llama-2-70b | 4 | roleplay | 18.252 | 14.0 | 6.0 | ok |
| 8 | Llama-2-70b | 4 | reasoning | 18.252 | 2.4 | 4.0 | **UNSAFE** |
| 9 | Mixtral-8x7B | 2 | general | 6.606 | 39.1 | 20.0 | ok |
| 10 | Mixtral-8x7B | 2 | codegen | 6.606 | 39.2 | 10.0 | ok |
| 11 | Mixtral-8x7B | 2 | roleplay | 6.606 | 38.6 | 6.0 | ok |
| 12 | Mixtral-8x7B | 2 | reasoning | 6.606 | 6.7 | 4.0 | ok |
| 13 | Qwen3-14B | 1 | general | 10.916 | 23.6 | 20.0 | ok |
| 14 | Qwen3-14B | 1 | codegen | 10.916 | 23.7 | 10.0 | ok |
| 15 | Qwen3-14B | 1 | roleplay | 10.916 | 23.4 | 6.0 | ok |
| 16 | Llama-3.1-8b | 1 | general | 6.257 | 41.2 | 20.0 | ok |
| 17 | DeepSeek-V3 | 8 | general | — | — | 20.0 | failed (OOM) |
| 18 | Scout-17B-16E | 2 | general | 9.948 | 25.9 | 20.0 | ok |
| 19 | Llama-3.1-8b | 1 | codegen | 6.257 | 41.4 | 10.0 | ok |
| 20 | Llama-3.1-8b | 1 | roleplay | 6.257 | 40.8 | 6.0 | ok |
| 21 | DeepSeek-V3 | 8 | codegen | — | — | 10.0 | failed (OOM) |
| 22 | DeepSeek-V3 | 8 | roleplay | — | — | 6.0 | failed (OOM) |
| 23 | Scout-17B-16E | 2 | codegen | 9.948 | 26.0 | 10.0 | ok |
| 24 | Scout-17B-16E | 2 | roleplay | 9.948 | 25.6 | 6.0 | ok |
| 25 | Llama-3.1-8b | 1 | general (mbt=1024) | 6.257 | 41.2 | 20.0 | ok |
| 26 | Llama-3.1-8b | 1 | general (mbt=8192) | 6.257 | 41.2 | 20.0 | ok |
| 27 | Llama-3.1-8b | 1 | general (cpu_offload) | 6.257 | 41.2 | 20.0 | ok |
| 28 | Llama-3.1-8b | 1 | general (gpu_mem=0.95) | 6.257 | 41.2 | 20.0 | ok |
| 29 | Llama-3.1-8b | 2 | general | 4.138 | 62.4 | 20.0 | ok |
| 30 | Mixtral-8x7B | 2 | general (mbt=1024) | 6.606 | 39.1 | 20.0 | ok |
| 31 | Mixtral-8x7B | 2 | general (mbt=8192) | 6.606 | 39.1 | 20.0 | ok |
| 32 | Mixtral-8x7B | 2 | general (cpu_offload) | 6.606 | 39.1 | 20.0 | ok |
| 33 | Mixtral-8x7B | 2 | general (gpu_mem=0.95) | 6.606 | 39.1 | 20.0 | ok |
| 34 | Mixtral-8x7B | 4 | general | 4.424 | 58.3 | 20.0 | ok |
| 35 | Mixtral-8x7B | 2 | general (dp=2, EP) | 6.606 | 39.1 | 20.0 | ok |
| 36 | Scout-17B-16E | 2 | general (dp=2, EP) | 9.948 | 25.9 | 20.0 | ok |
| 37 | Scout-17B-16E | 2 | general (dp=4, EP) | 9.948 | 25.9 | 20.0 | ok |
| 38 | Llama-2-7b-hf | 1 | general (dp=2) | 5.841 | 44.2 | 20.0 | ok |
| 50 | Llama-3.1-8b | 1 | reasoning | 6.257 | 7.1 | 4.0 | ok |
| 51 | Qwen3-14B | 1 | reasoning | 10.916 | 4.0 | 4.0 | **BORDERLINE** |
| 52 | DeepSeek-V3 | 8 | reasoning | — | — | 4.0 | failed (OOM) |
| 53 | Scout-17B-16E | 2 | reasoning | 9.948 | 4.4 | 4.0 | ok |

**Note:** For mbt sweep experiments (25-26, 30-31), the safe_rps is the same because
`max_num_seqs` (128) is the binding constraint, not `mbt`. Changing mbt from 1024 to 8192
doesn't change the safe rate when the seq limit dominates.

### Key findings

1. **Codellama-34b is rate-limited.** At 13.6 ms/token, it's the slowest model calibrated.
   The `general` workload peak (20 rps) exceeds the safe rate (19.0 rps) by 5%.
   The `reasoning` workload peak (4 rps) exceeds the safe rate (3.3 rps) by 21%.
   **Recommendation:** Lower peak rates to 18 rps (general) and 3 rps (reasoning).

2. **Qwen3-14B reasoning is borderline.** safe_rps = 4.0 exactly matches the peak rate,
   leaving zero headroom. **Recommendation:** Lower to 3.5 rps or accept the risk.

3. **FP8 MoE models:** DeepSeek-V3 cannot load on vLLM v0.15.1 with online dynamic
   quantization (deprioritized). Scout OOM resolved by switching to pre-quantized
   `RedHatAI/Llama-4-Scout-17B-16E-Instruct-FP8-dynamic` — calibration in progress.

4. **Llama-2-70b is the slowest model at 18.3 ms/token** (at TP4). General workload
   (safe 14.1 vs peak 20) and Reasoning (safe 2.4 vs peak 4) are both unsafe.
   **Recommendation:** Cap General to 14 rps, cap Reasoning to 2 rps, or deprioritize.

5. **Mixtral-8x7B tp4 (4.424 ms) is 33% faster than tp2 (6.606 ms)**, as expected from
   doubling GPU count. safe_rps = 58.3 vs peak 20 — very comfortable.

6. **All other models are comfortably within safe rates.** The smallest margin is
   Qwen3-14B general at 23.6 safe vs 20.0 peak (18% headroom).

7. **Reasoning workloads are the tightest** across all models due to 1,448 output tokens
   (6x more than other workloads). Each reasoning request occupies a decode slot for
   `1448 * decode_ms / 1000` seconds, consuming concurrent capacity much faster.

---

## Remaining work

- [x] ~~Mixtral-8x7B tp4 calibration~~ → 4.424 ms/token (pipeline run `cal-h100-kmdzs`)
- [x] ~~Scout calibration~~ → 9.948 ms/token (pipeline run `cal-h100-r8r2g`, RedHatAI FP8-dynamic checkpoint)
- [x] ~~Llama-2-70b calibration~~ → 18.252 ms/token (pipeline run `cal-h100-r8r2g`; partial download fixed)
- [x] ~~Resolve FP8 MoE OOM for Scout~~ → switched to `RedHatAI/Llama-4-Scout-17B-16E-Instruct-FP8-dynamic`
- [ ] Resolve FP8 MoE OOM for DeepSeek-V3 (no pre-quantized checkpoint; experiments deprioritized)
- [x] ~~Decide on rate adjustments~~ → Codellama-34b #1/#4, Llama-2-70b #5/#8 deprioritized as unsafe
- **All H100 calibrations complete** (9/10 configs; DeepSeek-V3 deprioritized)
