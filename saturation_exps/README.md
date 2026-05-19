# Saturation Experiment Campaign Generator

Generate BLIS campaign pipelines from saturation point experiments.

## Usage

```bash
python saturation_exps/generate_campaign.py --experiments exp1,exp3,exp5
```

This will:
1. Read `saturation_point_rps` and `final_precision_rps` from each experiment's `saturation_results.json`
2. Calculate two rates:
   - Saturation rate: `saturation_point_rps`
   - Overloaded rate: `saturation_point_rps + final_precision_rps`
3. Generate two variant directories per experiment:
   - `{exp}_saturation/` - Contains workload at saturation point
   - `{exp}_overloaded/` - Contains workload at overloaded rate
4. Each variant includes: `experiment.json`, `workload_{variant}.yaml`, `values.yaml`, `pipeline.yaml`, and `pipelinerun.yaml`
5. Both variants enable composite post-hoc detector for saturation analysis

**Saturation Detection**: All saturation experiments automatically enable BLIS's **composite post-hoc detector** during the observe phase (`--post-hoc-detector composite`). This detector combines rate deficit (1 - completions/arrivals) and latency trend (second-half vs first-half mean) to classify system state. Results are written to `saturation_analysis.json` in the observe output directory as `saturation.Result` JSON containing:
- `level`: Classification (STABLE, BACKLOGGED, or OVERLOADED)
- `score`: Combined saturation score (0.0 to 1.0)
- `confidence`: Detection confidence (0.0 to 1.0)
- `signals`: Detector-specific metrics (rate_deficit, latency_trend)

## Variants

Each saturation experiment generates two variants:

**Saturation variant** (`{exp}_saturation/`):
- Runs at the discovered `saturation_point_rps` (highest stable rate)
- Expected detector result: `STABLE` classification
- Validates that the saturation search correctly identified stable operation

**Overloaded variant** (`{exp}_overloaded/`):
- Runs at `saturation_point_rps + final_precision_rps` (slightly above threshold)
- Expected detector result: `OVERLOADED` or `BACKLOGGED` classification
- Confirms system behavior degrades above saturation point

Both variants:
- Deploy independent model instances (no interference)
- Use composite post-hoc detector for saturation analysis
- Output results to separate directories with complete observe data

## Input Requirements

Each experiment folder must contain:
- `experiment.json` - Model/hardware config with optional `harness` field
- `saturation_results.json` - Must have `result.saturation_point_rps`
- `saturation_*.yaml` - BLIS-native workload (exactly one YAML file)

## Running the Campaign

After generation, use the existing campaign runner:

```bash
# Run both variants of specific experiments
./blis-campaign/run-campaign.sh \
  --campaign saturation_exps/ \
  --hw H100 \
  --only exp1_saturation,exp1_overloaded,exp2_saturation,exp2_overloaded

# Run only saturation variants
./blis-campaign/run-campaign.sh \
  --campaign saturation_exps/ \
  --hw H100 \
  --only exp1_saturation,exp2_saturation,exp3_saturation

# Run only overloaded variants
./blis-campaign/run-campaign.sh \
  --campaign saturation_exps/ \
  --hw H100 \
  --only exp1_overloaded,exp2_overloaded,exp3_overloaded

# Run all variants
./blis-campaign/run-campaign.sh \
  --campaign saturation_exps/ \
  --hw H100
```

## Example

```bash
# Generate pipelines
python saturation_exps/generate_campaign.py --experiments exp1,exp2

# Output:
# Processing exp1...
#   ✓ Generated exp1_saturation (rate: 12.407 RPS - saturation point)
#   ✓ Generated exp1_overloaded (rate: 12.971 RPS - saturation + precision)
# Processing exp2...
#   ✓ Generated exp2_saturation (rate: X.XXX RPS - saturation point)
#   ✓ Generated exp2_overloaded (rate: Y.YYY RPS - saturation + precision)
#
# SUMMARY
# Processed 2 experiments: 2 succeeded, 0 failed

# Run campaign
./blis-campaign/run-campaign.sh --campaign saturation_exps/ --hw H100 --only exp1_saturation,exp1_overloaded
```

## Troubleshooting

**Error: "No workload YAML file found"**
- Ensure experiment folder contains exactly one `.yaml` file (excluding values.yaml, pipeline.yaml, pipelinerun.yaml)

**Error: "Model X must be a full HuggingFace ID"**
- Ensure the model field in `experiment.json` contains a full HuggingFace ID with org/model format (e.g., `meta-llama/Llama-3.1-8B-Instruct`)

**Error: "Hardware X not found in clusters.yaml"**
- Check that the hw field in `experiment.json` matches an entry in `blis-campaign/config/clusters.yaml`

**Error: "tektonc compilation failed"**
- Check that the harness field in `experiment.json` is valid ("orc", "blis-orc", or "inference-perf")
- Verify the template file exists in `tektoncsample/`
