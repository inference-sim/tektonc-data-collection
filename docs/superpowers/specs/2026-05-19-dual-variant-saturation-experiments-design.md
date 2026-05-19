# Dual-Variant Saturation Experiments Design

**Date**: 2026-05-19
**Status**: Approved
**Author**: Claude Sonnet 4.5

## Overview

Generate two parallel observe runs for each saturation experiment: one at the discovered saturation point (highest stable rate) and one at a slightly overloaded rate (saturation + precision). This captures system behavior at the saturation boundary and validates the saturation detector's classification.

## Goals

1. Run two independent observe phases per saturation experiment:
   - **Saturation variant**: Run at `saturation_point_rps` (highest stable rate)
   - **Overloaded variant**: Run at `saturation_point_rps + final_precision_rps` (slightly above saturation)

2. Deploy separate model instances for true independence (no interference between observations)

3. Collect complete observe results for both variants with composite detector analysis

4. Zero impact on regular blis-campaign experiments

## Architecture

### Input Structure

Each saturation experiment source directory (e.g., `saturation_exps/exp1/`) contains:
- `experiment.json` - Model/hardware configuration (including TP/DP settings)
- `saturation_results.json` - Contains `result.saturation_point_rps` and `result.final_precision_rps`
- `saturation_*.yaml` - BLIS-native workload specification

### Output Structure

Generator produces two peer directories for each experiment:

```
saturation_exps/
├── exp1/                          # Source (unchanged)
│   ├── experiment.json
│   ├── saturation_results.json
│   └── saturation_mmid_afternoon.yaml
├── exp1_saturation/               # Generated variant 1
│   ├── experiment.json            # id = "exp1_saturation"
│   ├── workload_saturation.yaml   # trace_rate = saturation_point_rps
│   ├── values.yaml
│   ├── pipeline.yaml
│   └── pipelinerun.yaml
└── exp1_overloaded/               # Generated variant 2
    ├── experiment.json            # id = "exp1_overloaded"
    ├── workload_overloaded.yaml   # trace_rate = saturation_point_rps + final_precision_rps
    ├── values.yaml
    ├── pipeline.yaml
    └── pipelinerun.yaml
```

### Rate Calculation

For each experiment:
1. Read `saturation_point_rps` from saturation_results.json (e.g., 12.407)
2. Read `final_precision_rps` from saturation_results.json (e.g., 0.564)
3. Generate two variants:
   - **Saturation**: `trace_rate = saturation_point_rps` (12.407 RPS)
   - **Overloaded**: `trace_rate = saturation_point_rps + final_precision_rps` (12.971 RPS)

Both rates are applied uniformly across all cohorts in the workload YAML.

## Data Flow

### Generation Phase

```
saturation_exps/generate_campaign.py --experiments exp1,exp2

For each experiment (exp1):
  1. Load exp1/experiment.json
  2. Load exp1/saturation_results.json
  3. Load exp1/saturation_*.yaml

  4. Calculate rates:
     saturation_rate = result.saturation_point_rps
     overloaded_rate = saturation_point_rps + final_precision_rps

  5. Generate exp1_saturation/:
     - Copy experiment.json with id="exp1_saturation"
     - Create workload_saturation.yaml with trace_rate=saturation_rate
     - Generate values.yaml (points to workload_saturation.yaml, detectSaturation=true)
     - Compile pipeline.yaml via tektonc
     - Generate pipelinerun.yaml

  6. Generate exp1_overloaded/:
     - Copy experiment.json with id="exp1_overloaded"
     - Create workload_overloaded.yaml with trace_rate=overloaded_rate
     - Generate values.yaml (points to workload_overloaded.yaml, detectSaturation=true)
     - Compile pipeline.yaml via tektonc
     - Generate pipelinerun.yaml
```

### Execution Phase

Both variants are independent experiments that can run:
- **Concurrently** on the cluster (using separate GPU resources)
- **Sequentially** if resource-constrained
- Each deploys its own model instance (TP/DP from experiment.json)
- No shared state or interference between variants

### Results Collection

After observe completes, each variant produces:

```
exp1_saturation/observe/
├── header.yaml                    # TraceV2 metadata
├── data.csv                       # Per-request trace data
├── itl.csv                        # Inter-token latency (optional)
├── workload_saturation.yaml       # Workload used (rate=12.407)
└── saturation_analysis.json       # Composite detector result (expected: STABLE)

exp1_overloaded/observe/
├── header.yaml
├── data.csv
├── itl.csv
├── workload_overloaded.yaml       # Workload used (rate=12.971)
└── saturation_analysis.json       # Composite detector result (expected: OVERLOADED)
```

## Usage

### Generate Variants

```bash
python saturation_exps/generate_campaign.py --experiments exp1,exp2,exp3

# Output:
# Processing exp1...
#   ✓ Generated exp1_saturation (rate: 12.407 RPS - saturation point)
#   ✓ Generated exp1_overloaded (rate: 12.971 RPS - saturation + precision)
# Processing exp2...
#   ✓ Generated exp2_saturation (rate: X.XXX RPS)
#   ✓ Generated exp2_overloaded (rate: Y.YYY RPS)
```

### Run Variants

```bash
# Run both variants of exp1
./blis-campaign/run-campaign.sh --campaign saturation_exps/ --hw H100 \
  --only exp1_saturation,exp1_overloaded

# Run only saturation points
./blis-campaign/run-campaign.sh --campaign saturation_exps/ --hw H100 \
  --only exp1_saturation,exp2_saturation,exp3_saturation

# Run only overloaded variants
./blis-campaign/run-campaign.sh --campaign saturation_exps/ --hw H100 \
  --only exp1_overloaded,exp2_overloaded,exp3_overloaded

# Run all variants
./blis-campaign/run-campaign.sh --campaign saturation_exps/ --hw H100
```

### Download Results

```bash
# Download saturation variant
kubectl exec -n diya deployment/busybox -- \
  tar czf - -C /data exp1_saturation | \
  tar xzf - -C saturation_exps/

# Download overloaded variant
kubectl exec -n diya deployment/busybox -- \
  tar czf - -C /data exp1_overloaded | \
  tar xzf - -C saturation_exps/
```

## Key Design Decisions

### 1. Independent Experiments (Not Loop Variants)

**Chosen approach**: Generate two separate experiment directories with complete pipeline files.

**Alternatives considered**:
- Tektonc loop with variant dimension: Adds complexity to template, harder to debug
- Meta-experiment with sub-pipelines: Over-engineered for this use case

**Rationale**: Treating variants as independent experiments is simpler, more explicit, and reuses all existing infrastructure without modification.

### 2. Separate Workload Files

**Chosen approach**: Generate `workload_saturation.yaml` and `workload_overloaded.yaml`.

**Alternatives considered**:
- Single workload with rate parameter: Requires passing rate through multiple layers
- Dynamic rate override: Less transparent, harder to audit

**Rationale**: Separate files make it crystal clear which rate is being used, easier to debug, and follows the existing pattern where workload YAML is the source of truth.

### 3. Separate Model Deployments

**Chosen approach**: Each variant deploys its own model instance.

**Alternatives considered**:
- Shared deployment with sequential runs: Simpler but can't run concurrently
- Shared deployment with concurrent runs: Observations interfere with each other

**Rationale**: Independence is critical for saturation experiments. Running both at different rates against the same instance would contaminate the measurements.

### 4. Explicit Variant Selection

**Chosen approach**: Users specify both variants explicitly in `--only` flag.

**Alternatives considered**:
- Prefix matching (`exp1` matches `exp1_*`): Risks breaking blis-campaign behavior
- Meta-experiment wrapper: Adds another layer of indirection

**Rationale**: Being explicit is safer and keeps run-campaign.sh unchanged, guaranteeing zero impact on regular blis-campaign usage.

## Backward Compatibility

**Zero impact on blis-campaign**:
- No changes to `tektoncsample/blis-orc/data_pipeline.yaml.j2`
- No changes to `blis-campaign/generate.py`
- No changes to `blis-campaign/run-campaign.sh`
- No changes to Tekton tasks

**Saturation experiments**:
- Original source directories (exp1/, exp2/) remain untouched
- Generated variants are new peer directories
- Existing saturation_exps/generate_campaign.py is extended but maintains current interface

## Validation Strategy

Expected composite detector results:
- **Saturation variant**: `level: STABLE` (score < 0.5) - running at discovered saturation point
- **Overloaded variant**: `level: OVERLOADED` or `BACKLOGGED` (score ≥ 0.5) - running above saturation threshold

If both variants classify as STABLE, the saturation search precision may need adjustment. If both classify as OVERLOADED, the saturation point may be underestimated.

## Implementation Notes

### Generator Changes (saturation_exps/generate_campaign.py)

Key modifications:
1. Change output path from `exp_name/` to `{exp_name}_saturation/` and `{exp_name}_overloaded/`
2. Add rate calculation: `overloaded_rate = saturation_point_rps + final_precision_rps`
3. Generate two workload files with different trace_rate values
4. Update experiment.json id field to include variant suffix
5. Call existing generation logic twice (once per variant)

### File Naming Convention

- Experiment IDs: `exp1_saturation`, `exp1_overloaded`
- Workload files: `workload_saturation.yaml`, `workload_overloaded.yaml`
- Pipeline names (DNS-1123): `blis-exp1-saturation-...`, `blis-exp1-overloaded-...`

### Success Criteria

1. Generator produces two directories per input experiment
2. Both variants compile successfully via tektonc
3. Both variants run independently on cluster
4. Saturation variant produces STABLE classification
5. Overloaded variant produces OVERLOADED/BACKLOGGED classification
6. Regular blis-campaign experiments continue working unchanged
