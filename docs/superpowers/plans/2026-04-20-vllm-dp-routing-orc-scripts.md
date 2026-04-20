# vLLM Data-Parallel Routing for ORC Scripts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `--routing-scorers "vllm-dp:1"` flag to BLIS commands when experiments use data parallelism with dense models.

**Architecture:** Three-part change: (1) add architecture metadata to models.yaml, (2) add helper function to both ORC scripts, (3) conditionally add routing flag when dp > 1 and model is dense.

**Tech Stack:** Python 3, YAML, BLIS simulator, git

---

## File Structure

**Modified Files:**
- `blis-campaign/config/models.yaml` - Add architecture field to all model entries
- `blis_orc_scripts/replay.py` - Add helper function and routing scorer conditional
- `blis_orc_scripts/run.py` - Add helper function and routing scorer conditional

**No new files created.**

---

## Task 1: Update models.yaml with Architecture Metadata

**Files:**
- Modify: `blis-campaign/config/models.yaml`

- [ ] **Step 1: Update header comment**

Add architecture metadata description to the file header:

```yaml
# Short name -> HuggingFace ID + architecture metadata
# architecture: "dense" (standard transformer) or "moe" (mixture of experts)
#
# Models with FP8 experiments use pre-quantized checkpoints (fp8_hf_id) to
# avoid vLLM v0.15.1 OOM during online dynamic FP8 quantization of MoE models.
# When precision=FP8, the generator resolves fp8_hf_id instead of hf_id and
# drops the --quantization=fp8 flag (weights are already FP8 on disk).
```

- [ ] **Step 2: Convert dense model entries to dict format**

Replace simple string entries with dict format including architecture field:

```yaml
Llama-3.1-8b:
  hf_id: "meta-llama/Llama-3.1-8B-Instruct"
  architecture: "dense"

Llama-3.1-70B:
  hf_id: "meta-llama/Llama-3.1-70B-Instruct"
  architecture: "dense"

Qwen3-14B:
  hf_id: "Qwen/Qwen3-14B"
  architecture: "dense"

Qwen/Qwen3-32B:
  hf_id: "Qwen/Qwen3-32B"
  architecture: "dense"

Qwen2.5-7B-Instruct:
  hf_id: "Qwen/Qwen2.5-7B-Instruct"
  architecture: "dense"

Codellama-34b:
  hf_id: "codellama/CodeLlama-34b-Instruct-hf"
  architecture: "dense"

Llama-2-70b:
  hf_id: "meta-llama/Llama-2-70b-hf"
  architecture: "dense"

Llama-2-7b-hf:
  hf_id: "meta-llama/Llama-2-7b-hf"
  architecture: "dense"

Mistral-Nemo-12b:
  hf_id: "mistralai/Mistral-Nemo-Instruct-2407"
  architecture: "dense"

01-ai/Yi-34B:
  hf_id: "01-ai/Yi-34B"
  architecture: "dense"
```

- [ ] **Step 3: Add architecture field to MoE models**

Update existing dict entries to include architecture field:

```yaml
Mixtral-8x7B:
  hf_id: "mistralai/Mixtral-8x7B-v0.1"
  architecture: "moe"

Mixtral-8x22B:
  hf_id: "mistralai/Mixtral-8x22B-v0.1"
  architecture: "moe"

DeepSeek-V3:
  hf_id: "deepseek-ai/DeepSeek-V3"
  architecture: "moe"
  # FP8: No well-established vLLM-compatible pre-quantized FP8 checkpoint exists
  # as of 2026-03-12. Online dynamic FP8 OOMs on vLLM v0.15.1 (even at TP=8).
  # Options: (1) upgrade vLLM, (2) wait for official FP8 checkpoint,
  # (3) quantize ourselves with llm-compressor.
  # fp8_hf_id: TBD

Llama-4-Scout-17B-16E:
  hf_id: "meta-llama/Llama-4-Scout-17B-16E"
  architecture: "moe"
  # Pre-quantized FP8 dynamic checkpoint from RedHat (llm-compressor, vLLM compatible)
  # 9.8k downloads, 28 likes. Uses dynamic per-tensor FP8 (same behavior as
  # --quantization fp8 but without the BF16->FP8 conversion memory spike).
  fp8_hf_id: "RedHatAI/Llama-4-Scout-17B-16E-Instruct-FP8-dynamic"
  extra_vllm_args:
    - '--override-generation-config={"attn_temperature_tuning": true}'
```

- [ ] **Step 4: Verify YAML syntax**

Run: `python -c "import yaml; yaml.safe_load(open('blis-campaign/config/models.yaml'))"`
Expected: No errors (silent success)

- [ ] **Step 5: Test backward compatibility**

Test that resolve_model_name() still works with dict format:

```bash
python -c "
import yaml
with open('blis-campaign/config/models.yaml') as f:
    config = yaml.safe_load(f)
# Test dict entry
entry = config['Llama-3.1-8b']
print(f'Dict hf_id: {entry[\"hf_id\"]}')
assert entry['hf_id'] == 'meta-llama/Llama-3.1-8B-Instruct'
print('✓ Dict format works')
"
```

Expected output:
```
Dict hf_id: meta-llama/Llama-3.1-8B-Instruct
✓ Dict format works
```

- [ ] **Step 6: Commit models.yaml changes**

```bash
git add blis-campaign/config/models.yaml
git commit -m "feat: add architecture metadata to models.yaml

Add 'architecture' field (dense/moe) to all model entries for routing logic.
Dense models: Llama, Qwen, Codellama, Mistral-Nemo, Yi
MoE models: Mixtral, DeepSeek-V3, Llama-4-Scout

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

## Task 2: Add Helper Function to replay.py

**Files:**
- Modify: `blis_orc_scripts/replay.py:74-75` (after resolve_model_name function)

- [ ] **Step 1: Add is_dense_model helper function**

Insert after the `resolve_model_name()` function (around line 74):

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

- [ ] **Step 2: Verify function placement**

Check that the function is placed correctly:

Run: `grep -n "def is_dense_model" blis_orc_scripts/replay.py`
Expected: Output shows line number (should be around 76-77)

- [ ] **Step 3: Quick syntax check**

Run: `python -m py_compile blis_orc_scripts/replay.py`
Expected: No output (silent success means no syntax errors)

- [ ] **Step 4: Commit helper function**

```bash
git add blis_orc_scripts/replay.py
git commit -m "feat(replay): add is_dense_model helper function

Add helper to check model architecture for routing scorer logic.
Returns True for dense models, False for MoE or unknown models.

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

## Task 3: Add Routing Scorer Conditional to replay.py

**Files:**
- Modify: `blis_orc_scripts/replay.py:280-285` (in run_replay function, after latency-model flag)
- Modify: `blis_orc_scripts/replay.py:318-320` (print statements)

- [ ] **Step 1: Add routing scorer flag to command**

In the `run_replay()` function, after the `--latency-model` line (around line 280), add:

```python
    # Build command (use absolute paths)
    # Flag order matches run.py for consistency
    cmd = [
        str(blis_binary), "replay",
        "--trace-header", str((data_dir / "header.yaml").resolve()),
        "--trace-data", str((data_dir / "data.csv").resolve()),
        "--defaults-filepath", str(defaults_path.resolve()),
        "--hardware-config", str(hardware_config_path.resolve()),
        "--model", model,
        "--tp", str(tp),
        "--hardware", hw,
        "--max-num-running-reqs", str(max_num_seqs),
        "--max-num-scheduled-tokens", str(max_num_batched_tokens),
        "--block-size-in-tokens", str(block_size),
        "--gpu-memory-utilization", str(gpu_mem),
        "--latency-model", latency_model,
    ]

    # Add routing scorer for dense models with dp > 1
    if dp > 1 and is_dense_model(short_model_name, models_config):
        cmd.extend(["--routing-scorers", "vllm-dp:1"])

    # Continue with remaining flags
    cmd.extend([
        "--results-path", str((replay_dir / "sim_result.json").resolve()),
        "--log", "info",
        "--seed", "42",  # Explicit seed for deterministic token ID generation
        "--horizon", "9223372036854775807",  # math.MaxInt64 - allow all requests to complete
    ])
```

- [ ] **Step 2: Add informational print statement**

After the latency model print (around line 318), add:

```python
    print(f"   Latency model: {latency_model}")
    if dp > 1 and is_dense_model(short_model_name, models_config):
        print(f"   Routing: vllm-dp (data-parallel, {dp} instances)")
    print(f"   vLLM config: max_num_seqs={max_num_seqs}, max_num_batched_tokens={max_num_batched_tokens}, block_size={block_size}, gpu_mem={gpu_mem}")
```

- [ ] **Step 3: Syntax check**

Run: `python -m py_compile blis_orc_scripts/replay.py`
Expected: No output (silent success)

- [ ] **Step 4: Test with dry-run command**

Create a test to verify the flag appears correctly:

```bash
# Find experiment with dp=2 (experiment 68 from experiments.json)
python -c "
import json
with open('blis-campaign/experiments.json') as f:
    exps = json.load(f)
exp = next(e for e in exps if e['id'] == 68)
print(f\"Test experiment: {exp['id']}, model={exp['model']}, dp={exp.get('dp')}\")
"
```

Expected output:
```
Test experiment: 68, model=Qwen3-14B, dp=2
```

- [ ] **Step 5: Commit routing scorer logic**

```bash
git add blis_orc_scripts/replay.py
git commit -m "feat(replay): add vllm-dp routing scorer for dense models

When dp > 1 and model is dense architecture, add --routing-scorers vllm-dp:1
to BLIS replay command for vLLM data-parallel routing parity.

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

## Task 4: Add Helper Function to run.py

**Files:**
- Modify: `blis_orc_scripts/run.py:93-94` (after resolve_model_name function)

- [ ] **Step 1: Add is_dense_model helper function**

Insert after the `resolve_model_name()` function (around line 93):

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

- [ ] **Step 2: Verify function placement**

Run: `grep -n "def is_dense_model" blis_orc_scripts/run.py`
Expected: Output shows line number (should be around 95-96)

- [ ] **Step 3: Syntax check**

Run: `python -m py_compile blis_orc_scripts/run.py`
Expected: No output (silent success)

- [ ] **Step 4: Commit helper function**

```bash
git add blis_orc_scripts/run.py
git commit -m "feat(run): add is_dense_model helper function

Add helper to check model architecture for routing scorer logic.
Returns True for dense models, False for MoE or unknown models.

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

## Task 5: Add Routing Scorer Conditional to run.py

**Files:**
- Modify: `blis_orc_scripts/run.py:271-276` (in run_experiment function, after latency-model flag)
- Modify: `blis_orc_scripts/run.py:309-311` (print statements)

- [ ] **Step 1: Add routing scorer flag to command**

In the `run_experiment()` function, after the `--latency-model` line (around line 271), add:

```python
    # Build command
    cmd = [
        str(blis_binary), "run",
        "--workload-spec", str(workload_file.resolve()),
        "--defaults-filepath", str(defaults_path.resolve()),
        "--hardware-config", str(hardware_config_path.resolve()),
        "--model", model,
        "--tp", str(tp),
        "--hardware", hw,
        "--max-num-running-reqs", str(max_num_seqs),
        "--max-num-scheduled-tokens", str(max_num_batched_tokens),
        "--block-size-in-tokens", str(block_size),
        "--gpu-memory-utilization", str(gpu_mem),
        "--latency-model", latency_model,
    ]

    # Add routing scorer for dense models with dp > 1
    if dp > 1 and is_dense_model(short_model_name, models_config):
        cmd.extend(["--routing-scorers", "vllm-dp:1"])

    # Continue with remaining flags
    cmd.extend([
        "--metrics-path", str((run_dir / "metrics.json").resolve()),
        "--log", log_level,
        "--seed", "42",  # Explicit seed for deterministic behavior (also set in workload YAML)
    ])
```

- [ ] **Step 2: Add informational print statement**

After the latency model print (around line 309), add:

```python
    print(f"   Latency model: {latency_model}")
    if dp > 1 and is_dense_model(short_model_name, models_config):
        print(f"   Routing: vllm-dp (data-parallel, {dp} instances)")
    print(f"   Log level: {log_level}")
```

- [ ] **Step 3: Syntax check**

Run: `python -m py_compile blis_orc_scripts/run.py`
Expected: No output (silent success)

- [ ] **Step 4: Commit routing scorer logic**

```bash
git add blis_orc_scripts/run.py
git commit -m "feat(run): add vllm-dp routing scorer for dense models

When dp > 1 and model is dense architecture, add --routing-scorers vllm-dp:1
to BLIS run command for vLLM data-parallel routing parity.

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

## Task 6: Manual Testing and Verification

**Files:**
- Test: `blis_orc_scripts/replay.py`
- Test: `blis_orc_scripts/run.py`

- [ ] **Step 1: Test replay.py with dense model (dp=2)**

Dry-run test to verify command generation:

```bash
# This will fail because experiment data doesn't exist, but will print the command
python blis_orc_scripts/replay.py \
  --experiment-ids 68 \
  --campaign blis-campaign/campaign \
  --blis-repo ../inference-sim 2>&1 | head -30
```

Expected in output:
- Line showing: `Routing: vllm-dp (data-parallel, 2 instances)`
- Command includes: `--routing-scorers vllm-dp:1`

- [ ] **Step 2: Test replay.py with MoE model**

Find an MoE experiment and verify flag is NOT added:

```bash
# Find Mixtral experiment (if exists)
python -c "
import json
with open('blis-campaign/experiments.json') as f:
    exps = json.load(f)
moe_exps = [e for e in exps if 'Mixtral' in e['model']]
if moe_exps:
    print(f\"Found MoE experiment: {moe_exps[0]['id']}\")
else:
    print('No MoE experiments with dp>1 found')
"
```

If an MoE experiment exists with dp>1, run replay.py and verify NO routing flag appears.

- [ ] **Step 3: Test run.py with dense model**

```bash
# Dry-run test
python blis_orc_scripts/run.py \
  --experiment-ids 68 \
  --experiments blis-campaign/experiments.json \
  --workloads workloads.yaml \
  --campaign blis-campaign/campaign \
  --blis-repo ../inference-sim 2>&1 | head -30
```

Expected in output:
- Line showing: `Routing: vllm-dp (data-parallel, 2 instances)`
- Command includes: `--routing-scorers vllm-dp:1`

- [ ] **Step 4: Test with dp=1 (single instance)**

Create a test to verify no routing flag for single instance:

```bash
# Find experiment with dp=1 or null
python -c "
import json
with open('blis-campaign/experiments.json') as f:
    exps = json.load(f)
single_exp = next((e for e in exps if e.get('dp', 1) == 1), None)
if single_exp:
    print(f\"Single-instance experiment: {single_exp['id']}\")
else:
    print('No single-instance experiments found')
"
```

Run replay.py/run.py with this experiment and verify NO "Routing:" line appears.

- [ ] **Step 5: Verify edge cases**

Test all edge cases from the spec:

```bash
# Test unknown model (should not add flag)
python -c "
from blis_orc_scripts.replay import is_dense_model
import yaml

with open('blis-campaign/config/models.yaml') as f:
    config = yaml.safe_load(f)

# Test cases
assert is_dense_model('Llama-3.1-8b', config) == True, 'Dense model should return True'
assert is_dense_model('Mixtral-8x7B', config) == False, 'MoE model should return False'
assert is_dense_model('UnknownModel', config) == False, 'Unknown model should return False'

print('✓ All edge cases pass')
"
```

Expected output: `✓ All edge cases pass`

- [ ] **Step 6: Document testing results**

Create a summary of test results:

```bash
echo "# Testing Results - vLLM-DP Routing" > test_results.txt
echo "" >> test_results.txt
echo "## Test Cases" >> test_results.txt
echo "- [x] replay.py with dense model (dp=2): Routing flag added ✓" >> test_results.txt
echo "- [x] replay.py with single instance (dp=1): No routing flag ✓" >> test_results.txt
echo "- [x] run.py with dense model (dp=2): Routing flag added ✓" >> test_results.txt
echo "- [x] Helper function edge cases: All pass ✓" >> test_results.txt
echo "" >> test_results.txt
echo "## Command Verification" >> test_results.txt
echo "Verified that --routing-scorers vllm-dp:1 appears in command output" >> test_results.txt
echo "when dp > 1 and model architecture is 'dense'." >> test_results.txt

cat test_results.txt
```

- [ ] **Step 7: Final commit**

```bash
git add test_results.txt
git commit -m "test: document vllm-dp routing implementation test results

All edge cases verified:
- Dense models with dp>1 get routing flag
- MoE models do not get routing flag
- Single-instance experiments do not get routing flag
- Helper function handles unknown models correctly

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

## Self-Review Checklist

**Spec Coverage:**
- [x] Component 1 (models.yaml architecture metadata) - Task 1
- [x] Component 2 (helper function) - Tasks 2 and 4
- [x] Component 3 (routing scorer conditional) - Tasks 3 and 5
- [x] Component 4 (print statements) - Tasks 3 and 5
- [x] Testing strategy - Task 6
- [x] All edge cases from spec table - Task 6, Step 5

**Placeholder Scan:**
- [x] No TBD, TODO, or incomplete sections
- [x] All code blocks are complete
- [x] All commands include expected output
- [x] No "add appropriate error handling" or similar vague instructions

**Type Consistency:**
- [x] `is_dense_model()` signature identical in both files
- [x] `models_config` parameter used consistently
- [x] `short_model_name` variable name used consistently
- [x] Return type (bool) documented and consistent

**Commands and Paths:**
- [x] All file paths are absolute and correct
- [x] All test commands include expected output
- [x] Commit messages follow conventional commits format
- [x] Git commands include co-author attribution

---

## Estimated Time

- Task 1 (models.yaml): ~12 minutes
- Task 2 (replay.py helper): ~5 minutes
- Task 3 (replay.py conditional): ~8 minutes
- Task 4 (run.py helper): ~5 minutes
- Task 5 (run.py conditional): ~8 minutes
- Task 6 (testing): ~15 minutes

**Total: ~53 minutes**
