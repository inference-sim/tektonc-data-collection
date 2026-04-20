# Add vLLM Data-Parallel Routing to BLIS ORC Scripts

**Date:** 2026-04-20
**Status:** Design Approved

## Problem Statement

The BLIS ORC scripts (`blis_orc_scripts/replay.py` and `blis_orc_scripts/run.py`) currently do not specify routing policy when running experiments with data parallelism (`dp > 1`). This causes BLIS to use its default routing profile (precise-prefix-cache + queue-depth + kv-utilization), which differs from vLLM's actual data-parallel routing behavior.

For accurate simulation parity with multi-instance vLLM deployments, BLIS needs to use the `vllm-dp` scorer, which replicates vLLM's `waiting × 4 + running` load-balancing formula.

## Goals

1. Add `--routing-scorers "vllm-dp:1"` to BLIS commands when `dp > 1`
2. Only apply this for **dense models** (not MoE architectures)
3. Make model architecture metadata explicit in `models.yaml`
4. Maintain backward compatibility with existing experiments

## Non-Goals

- Changing routing behavior for single-instance experiments (`dp = 1` or `dp = null`)
- Implementing custom routing for MoE models (out of scope)
- Modifying BLIS routing scorer implementation (already exists)

## Design

### Component 1: Model Architecture Metadata

**File:** `blis-campaign/config/models.yaml`

Add explicit `architecture` field to all model entries to distinguish dense from MoE models.

**Changes:**
- Convert simple string entries to dict format with `hf_id` and `architecture` fields
- Add `architecture: "dense"` for dense models (Llama, Qwen, Codellama, etc.)
- Add `architecture: "moe"` for MoE models (Mixtral, DeepSeek-V3, Llama-4-Scout)

**Example:**
```yaml
# Before (simple string)
Llama-3.1-8b: "meta-llama/Llama-3.1-8B-Instruct"

# After (explicit architecture)
Llama-3.1-8b:
  hf_id: "meta-llama/Llama-3.1-8B-Instruct"
  architecture: "dense"

# MoE example
Mixtral-8x7B:
  hf_id: "mistralai/Mixtral-8x7B-v0.1"
  architecture: "moe"
```

**Model Classification:**
- **Dense:** Llama-3.1-8b, Llama-3.1-70B, Qwen3-14B, Qwen3-32B, Qwen2.5-7B-Instruct, Codellama-34b, Llama-2-70b, Llama-2-7b-hf, Mistral-Nemo-12b, 01-ai/Yi-34B
- **MoE:** Mixtral-8x7B, Mixtral-8x22B, DeepSeek-V3, Llama-4-Scout-17B-16E

**Backward Compatibility:**
- Existing code using `resolve_model_name()` continues to work (reads `hf_id` field)
- New code checks `architecture` field, defaults to "dense" if missing

### Component 2: Helper Function

**Files:** Both `blis_orc_scripts/replay.py` and `blis_orc_scripts/run.py`

Add a helper function to check if a model is dense architecture:

```python
def is_dense_model(short_name, models_config):
    """Check if model is a dense architecture (not MoE).

    Args:
        short_name: Short model name like "Llama-3.1-8b"
        models_config: Dict from models.yaml

    Returns:
        bool: True if dense, False if MoE or architecture unknown
    """
    if short_name not in models_config:
        # Unknown model, default to False (don't add routing scorer)
        return False

    entry = models_config[short_name]
    if isinstance(entry, dict):
        # Check architecture field, default to "dense" for backward compatibility
        return entry.get("architecture", "dense") == "dense"

    # Simple string entry (legacy format), assume dense
    return True
```

**Design Decisions:**
- Default to `False` for unknown models (conservative: don't add flag unless we're sure)
- Default to `"dense"` for dict entries without architecture (backward compatibility)
- Simple string entries treated as dense (legacy support)

### Component 3: Routing Scorer Conditional

**Location:** Both `replay.py` (line ~267-284) and `run.py` (line ~259-275)

Add conditional logic after reading `dp` from experiment config:

```python
# Add routing scorer for dense models with dp > 1
if dp > 1 and is_dense_model(short_model_name, models_config):
    cmd.extend(["--routing-scorers", "vllm-dp:1"])
```

**Placement in command array:**
- After `--latency-model` flag
- Before output path flags (`--results-path` or `--metrics-path`)
- This keeps related simulation config flags together

**Why this condition?**
- `dp > 1`: Multi-instance deployment requires routing policy
- `is_dense_model()`: vLLM-DP routing only applies to dense models
  - MoE models have internal expert routing that may interact with data-parallel routing
  - The `waiting × 4 + running` formula assumes uniform compute, which may not hold for MoE

### Component 4: Print Statements

Add informational output when the flag is added:

**In `replay.py` (line ~318):**
```python
print(f"   Latency model: {latency_model}")
if dp > 1 and is_dense_model(short_model_name, models_config):
    print(f"   Routing: vllm-dp (data-parallel, {dp} instances)")
```

**In `run.py` (line ~309):**
```python
print(f"   Latency model: {latency_model}")
if dp > 1 and is_dense_model(short_model_name, models_config):
    print(f"   Routing: vllm-dp (data-parallel, {dp} instances)")
```

## Edge Cases

| Scenario | Behavior | Rationale |
|----------|----------|-----------|
| `dp = null` or `dp = 1` | No routing flag | Single instance, routing not applicable |
| `dp > 1`, dense model | Add `--routing-scorers "vllm-dp:1"` | Primary use case |
| `dp > 1`, MoE model | No routing flag | MoE routing may differ from dense |
| Model not in config | No routing flag | Conservative default |
| Architecture field missing | Treat as dense (legacy) | Backward compatibility |

## Testing Strategy

**Manual Testing:**
1. Run `replay.py` with a dense model experiment where `dp = 2` → verify flag appears in command
2. Run `run.py` with a dense model experiment where `dp = 4` → verify flag appears in command
3. Run with MoE model (Mixtral) where `dp = 2` → verify flag does NOT appear
4. Run with `dp = 1` → verify flag does NOT appear

**Verification:**
- Check stdout logs for "Routing: vllm-dp" message
- Check full command printout includes `--routing-scorers vllm-dp:1`
- Run end-to-end replay/run and ensure BLIS doesn't error on the flag

## Alternatives Considered

### Alternative 1: Heuristic Name Detection

Detect MoE models by pattern matching on names ("Mixtral", "DeepSeek-V", "-[0-9]+E").

**Rejected because:**
- Brittle: breaks when new MoE models are added
- Implicit: architecture not documented
- Harder to maintain: regex patterns need updates for each new model family

### Alternative 2: Always Add Flag for dp > 1

Add `--routing-scorers "vllm-dp:1"` for all models when `dp > 1`, regardless of architecture.

**Rejected because:**
- May produce incorrect results for MoE models
- vLLM's routing behavior for MoE + data-parallel is not well-documented
- Conservative approach is safer: only add when we know it's correct

### Alternative 3: CLI Override Flag

Add `--routing-policy` argument to replay.py/run.py for user control.

**Rejected because:**
- Over-engineered for current needs
- Experiments already specify `dp`, which implies routing requirements
- Can be added later if needed without breaking existing code

## Implementation Plan

1. **Update models.yaml** (~10 min)
   - Convert string entries to dicts
   - Add architecture field to all models
   - Test that existing name resolution still works

2. **Add helper function** (~5 min)
   - Copy `is_dense_model()` to both replay.py and run.py
   - Place after `resolve_model_name()` function

3. **Add conditional logic** (~10 min)
   - Insert routing scorer check in both scripts
   - Add print statements for visibility

4. **Test with real experiments** (~15 min)
   - Run replay.py with experiment 68 (Qwen3-14B, dp=2)
   - Run run.py with experiment 68
   - Verify command output and logs

5. **Commit changes** (~5 min)
   - Git add, commit with descriptive message

**Total time:** ~45 minutes

## Success Criteria

- [ ] All models in models.yaml have explicit architecture field
- [ ] `is_dense_model()` helper returns correct results for test cases
- [ ] `replay.py` adds routing flag for dense + dp>1 experiments
- [ ] `run.py` adds routing flag for dense + dp>1 experiments
- [ ] MoE models with dp>1 do NOT get routing flag
- [ ] Single-instance experiments (dp≤1) do NOT get routing flag
- [ ] BLIS commands run successfully with new flag
- [ ] Logs show "Routing: vllm-dp" message when applicable

## Documentation Impact

**CLAUDE.md:**
No changes needed. The modification is internal to ORC scripts.

**models.yaml comments:**
Update header comment to mention architecture field:
```yaml
# Short name -> HuggingFace ID + architecture metadata
# architecture: "dense" (standard transformer) or "moe" (mixture of experts)
```

## References

- BLIS routing guide: `../inference-sim/docs/guide/routing.md`
- vLLM-DP scorer design: `../inference-sim/docs/plans/vllm-dp-scorer-design.md`
- Experiments config: `blis-campaign/experiments.json`
- Models config: `blis-campaign/config/models.yaml`
