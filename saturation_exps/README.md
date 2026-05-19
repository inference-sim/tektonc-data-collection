# Saturation Experiment Campaign Generator

Generate BLIS campaign pipelines from saturation point experiments.

## Usage

```bash
python saturation_exps/generate_campaign.py --experiments exp1,exp3,exp5
```

This will:
1. Read `saturation_point_rps` from each experiment's `saturation_results.json`
2. Update all cohort `trace_rate` values in the workload YAML
3. Generate `values.yaml`, `pipeline.yaml`, and `pipelinerun.yaml` with saturation detection enabled
4. Write all outputs to the experiment folder

**Saturation Detection**: All saturation experiments automatically enable BLIS's **composite post-hoc detector** during the observe phase (`--post-hoc-detector composite`). This detector combines rate deficit (1 - completions/arrivals) and latency trend (second-half vs first-half mean) to classify system state. Results are written to `saturation_analysis.json` in the observe output directory as `saturation.Result` JSON containing:
- `level`: Classification (STABLE, BACKLOGGED, or OVERLOADED)
- `score`: Combined saturation score (0.0 to 1.0)
- `confidence`: Detection confidence (0.0 to 1.0)
- `signals`: Detector-specific metrics (rate_deficit, latency_trend)

## Input Requirements

Each experiment folder must contain:
- `experiment.json` - Model/hardware config with optional `harness` field
- `saturation_results.json` - Must have `result.saturation_point_rps`
- `saturation_*.yaml` - BLIS-native workload (exactly one YAML file)

## Running the Campaign

After generation, use the existing campaign runner:

```bash
# Run specific experiments
./blis-campaign/run-campaign.sh \
  --campaign saturation_exps/ \
  --hw H100 \
  --only 1,3,5

# Run a range of experiments
./blis-campaign/run-campaign.sh \
  --campaign saturation_exps/ \
  --hw H100 \
  --range 1-10
```

## Example

```bash
# Generate pipelines
python saturation_exps/generate_campaign.py --experiments exp1,exp2

# Output:
# Processing exp1...
#   ✓ Generated pipeline files in saturation_exps/exp1
# Processing exp2...
#   ✓ Generated pipeline files in saturation_exps/exp2
#
# SUMMARY
# Processed 2 experiments: 2 succeeded, 0 failed

# Run campaign
./blis-campaign/run-campaign.sh --campaign saturation_exps/ --hw H100 --only 1,2
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
