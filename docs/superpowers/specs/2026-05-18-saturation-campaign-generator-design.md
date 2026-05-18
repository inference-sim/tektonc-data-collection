# Saturation Campaign Generator Design

**Date:** 2026-05-18
**Status:** Approved

## Overview

A tool to transform saturation point experiment folders into BLIS campaign-ready directories. Takes saturation experiments that have already been run through binary search, reads their saturation point RPS, and generates Tekton pipeline YAML for validation runs at the discovered saturation rate.

## Goals

- Easy validation of saturation point findings on real hardware
- Batch process multiple saturation experiments (exp1, exp2, exp3, etc.)
- Reuse exact workload definitions from saturation experiments (no dynamic generation)
- Compatible with existing `run-campaign.sh` infrastructure

## Non-Goals

- Dynamic workload generation (we use pre-existing workload YAML files)
- Rate multipliers or manual overrides (always use exact saturation point)
- Automatic experiment discovery (explicit experiment list required)

## Architecture

### Script Location
`saturation_exps/generate_campaign.py`

### CLI Interface
```bash
python saturation_exps/generate_campaign.py --experiments exp1,exp3,exp5
```

**Arguments:**
- `--experiments` (required): Comma-separated list of experiment folder names

### Input Files (per experiment)

Each experiment folder must contain:

```
saturation_exps/expN/
  experiment.json               # Model/hardware config with "harness" field
  saturation_results.json       # Contains "result.saturation_point_rps"
  saturation_*.yaml             # BLIS-native workload (exactly one YAML file)
```

**Shared config files:**
- `blis-campaign/models.yaml` - Model image/config lookups
- `blis-campaign/clusters.yaml` - Cluster endpoint/namespace
- `tektoncsample/blis-orc/` or `tektoncsample/blis-inference-perf/` - Pipeline templates

### Output Files (per experiment)

Generated in the same experiment folder:

```
saturation_exps/expN/
  saturation_*.yaml             # MODIFIED: trace_rate updated to saturation RPS
  values.yaml                   # NEW: Tekton values for pipeline compilation
  pipeline.yaml                 # NEW: Compiled Tekton Pipeline
  pipelinerun.yaml              # NEW: PipelineRun template
```

## Components

### 1. Workload Rate Updater

**Purpose:** Update workload YAML to use saturation point RPS.

**Algorithm:**
1. Load workload YAML file from experiment folder
2. Read `saturation_point_rps` from `saturation_results.json`
3. Iterate through all cohorts in `cohorts` array
4. For each cohort: update `spike.trace_rate` to `saturation_point_rps`
5. Write modified workload back to same file

**YAML structure:**
```yaml
cohorts:
  - id: afternoon-background
    spike:
      trace_rate: 12.406919642857135  # <-- UPDATE THIS
  - id: afternoon-batch
    spike:
      trace_rate: 12.406919642857135  # <-- UPDATE THIS
  # ... all other cohorts
```

**Note:** All cohorts get the same `trace_rate` value (the saturation point).

### 2. Workload File Discovery

**Purpose:** Find the BLIS workload YAML file in the experiment folder.

**Algorithm:**
1. List all `*.yaml` files in experiment folder
2. Exclude generated files: `values.yaml`, `pipeline.yaml`, `pipelinerun.yaml`
3. If exactly 1 file remains → use it
4. If 0 files → Error: "No workload YAML file found"
5. If >1 files → Error: "Multiple workload files found: [list]"

### 3. Values Generator

**Purpose:** Create `values.yaml` for tektonc compilation.

**Algorithm:**
1. Load `experiment.json` to get: model, hw, tp, dp, scheduling, harness
2. Load model config from `blis-campaign/models.yaml` using model name
3. Load cluster config from `blis-campaign/clusters.yaml` using hw
4. Construct values dict:
   - Model deployment parameters (image, vLLM args, GPU count)
   - Cluster target (context, namespace)
   - Workload file path (absolute or relative)
   - Harness selection (default to "orc" if not specified)
5. Write `values.yaml` to experiment folder

**Key fields in values.yaml:**
- `model.name`, `model.image`, `model.vllm_args`
- `cluster.context`, `cluster.namespace`
- `workload_file` - path to the workload YAML
- `harness` - "orc" or "inference-perf"

### 4. Pipeline Compiler

**Purpose:** Generate Tekton Pipeline and PipelineRun YAML.

**Algorithm:**
1. Read `harness` field from `experiment.json` (default: "orc")
2. Select template based on harness:
   - `orc` → `tektoncsample/blis-orc/data_pipeline.yaml.j2`
   - `inference-perf` → `tektoncsample/blis-inference-perf/data_pipeline.yaml.j2`
3. Call tektonc:
   ```bash
   python tektonc/tektonc.py \
     -t <template> \
     -f saturation_exps/expN/values.yaml \
     -o saturation_exps/expN/pipeline.yaml
   ```
4. Generate `pipelinerun.yaml` with experiment-specific naming:
   - Name: `saturation-exp{id}-{timestamp}`
   - PipelineRef: matches pipeline name from compilation

## Data Flow

```
Input:
  experiment.json → model, hw, tp, scheduling, harness
  saturation_results.json → saturation_point_rps
  saturation_*.yaml → workload cohorts
  blis-campaign/models.yaml → model configs
  blis-campaign/clusters.yaml → cluster configs

Processing:
  1. Parse --experiments list
  2. For each experiment:
     a. Load input files
     b. Update workload trace_rate for all cohorts
     c. Generate values.yaml
     d. Compile pipeline.yaml via tektonc
     e. Generate pipelinerun.yaml

Output:
  saturation_exps/expN/values.yaml
  saturation_exps/expN/pipeline.yaml
  saturation_exps/expN/pipelinerun.yaml
  saturation_exps/expN/saturation_*.yaml (modified)
```

## Integration with Campaign Runner

The generated experiments are compatible with the existing campaign runner.

**Usage:**
```bash
# Step 1: Generate pipelines
python saturation_exps/generate_campaign.py --experiments exp1,exp3,exp5

# Step 2: Run campaign with filtering
./blis-campaign/run-campaign.sh \
  --campaign saturation_exps/ \
  --hw H100 \
  --only 1,3,5
```

**Runner filters by numeric ID:**
- `experiment.json` contains `"id": 1` (numeric)
- `--only 1,3,5` matches experiments with these IDs
- `--range 1-5` matches experiments with ID 1 through 5

**Directory structure compatibility:**
- Runner scans `--campaign` directory for subdirectories
- Each subdirectory must contain: `experiment.json`, `pipeline.yaml`, `pipelinerun.yaml`
- Generated saturation experiments meet these requirements

## Error Handling

### Missing Files
- No experiment folder → Skip with error message, continue to next
- No `experiment.json` → Fail experiment: "Missing experiment.json in {folder}"
- No `saturation_results.json` → Fail experiment: "Missing saturation_results.json"
- No workload YAML → Fail experiment: "No workload YAML file found (expected saturation_*.yaml)"
- Multiple workload YAMLs → Fail experiment: "Multiple workload files found: [list]"

### Invalid Data
- Malformed JSON/YAML → Show parse error, fail experiment
- Missing required fields in `experiment.json` → Fail: "Missing required field: {field}"
- Missing `saturation_point_rps` → Fail: "No saturation_point_rps in saturation_results.json"
- Missing `cohorts` array → Fail: "Invalid workload: no cohorts array"
- Cohort missing `spike.trace_rate` → Fail: "Cohort {id} missing spike.trace_rate"

### tektonc Compilation
- tektonc failure → Capture stderr, display error, fail experiment
- Invalid template path → Fail: "Template not found: {template}"

### Partial Success
- Script continues processing even if some experiments fail
- Final summary: "Processed X experiments: Y succeeded, Z failed"
- Exit code 0 if any succeeded, 1 if all failed

## Implementation Notes

### Rate Update Logic
- All cohorts in a workload get the same `trace_rate` (the saturation point)
- Original trace_rate values are overwritten (this is intentional)
- The workload file is the source of truth and should be modified in-place

### Harness Selection
- Read `harness` field from `experiment.json`
- Default to `"orc"` if field is missing
- Supported values: `"orc"`, `"inference-perf"`, `"blis-orc"` (alias for orc)

### Template Paths
```python
TEMPLATES = {
    "orc": "tektoncsample/blis-orc/data_pipeline.yaml.j2",
    "blis-orc": "tektoncsample/blis-orc/data_pipeline.yaml.j2",
    "inference-perf": "tektoncsample/blis-inference-perf/data_pipeline.yaml.j2"
}
```

### Values.yaml Structure
```yaml
model:
  name: Llama-3.1-8B-Instruct
  image: vllm/vllm-openai:latest
  vllm_args: "--tensor-parallel-size 1 ..."
  tp: 1
  dp: 1

cluster:
  context: gke_project_us-central1-a_cluster
  namespace: blis

workload_file: saturation_exps/exp1/saturation_mmid_afternoon.yaml
harness: orc
```

## Testing Considerations

### Unit Tests
- Workload rate update with various cohort structures
- Workload file discovery (0, 1, or multiple YAMLs)
- Values generation with different experiment configs
- Error handling for missing/malformed files

### Integration Tests
- End-to-end generation for a sample experiment
- Verify generated YAML is valid Tekton syntax
- Verify trace_rate correctly updated in workload
- Test with both orc and inference-perf harnesses

### Manual Validation
- Generate for a real saturation experiment
- Deploy with run-campaign.sh
- Verify experiment runs at expected saturation RPS
- Verify results match saturation point findings

## Future Enhancements (Out of Scope)

- Rate multipliers (run at 90% or 110% of saturation)
- Automatic experiment discovery (scan saturation_exps/)
- Workload validation against BLIS schema
- Parallel generation for large experiment sets
- Dry-run mode (preview without modifying files)
