# Local Replay/Calibrate Workflow

The ORC pipeline has been updated to run **observe on the cluster** and **replay/calibrate locally**. This allows you to:
- Collect real trace data from the cluster (GPU-accelerated)
- Run simulation locally (CPU-only, no GPU needed)
- Iterate on calibration without cluster resources

## Pipeline Structure

### On Cluster (Tekton)
```
download-model → install-blis → deploy-model → create-exp-config
                                     ↓
                              orc-observe (collect trace)
                                     ↓
                              delete-model (free GPU)
                                     ↓
                        download-observe-data (validate)
```

### Locally (Python scripts)
```
Download trace data
        ↓
   replay.py (simulation)
        ↓
   calibrate.py (compare real vs simulated)
```

## Workflow Steps

### 1. Run Observe on Cluster

```bash
# Generate and launch campaign
python -m blis-campaign generate --experiments blis-campaign/experiments.json --only 68 --output blis-campaign/campaign/
./blis-campaign/run-campaign.sh --campaign blis-campaign/campaign/ --hw H100 --only 68

# Or manually
kubectl apply -f blis-campaign/campaign/68-llama-3-1-8b-h100-general-lite/pipeline.yaml
kubectl apply -f blis-campaign/campaign/68-llama-3-1-8b-h100-general-lite/pipelinerun.yaml
```

### 2. Observe Data Downloaded Automatically

The campaign runner automatically downloads observe data after experiments succeed using `kubectl exec` + tar pipe to:

```
blis-campaign/campaign/<exp-dir>/data/<exp-id-tp-dp>/observe/
```

**If automatic download fails**, you can manually download:

```bash
# Manual download (if needed)
kubectl exec -n diya deployment/busybox -- tar czf - -C /data 68-llama-3-1-8b-tp1-general-lite-1-1 | \
  tar xzf - -C blis-campaign/campaign/68-llama-3-1-8b-h100-general-lite/data/
```

Downloaded structure:
```
blis-campaign/campaign/68-llama-3-1-8b-h100-general-lite/
└── data/
    └── 68-llama-3-1-8b-tp1-general-lite-1-1/
        ├── observe/
        │   ├── header.yaml    # Trace metadata
        │   ├── data.csv       # Request trace (5000+ records)
        │   └── workload.yaml  # Workload spec used
        ├── exp-config.yaml    # Experiment configuration
        ├── vllm_logging.json  # vLLM logging config
        └── vllm.log           # vLLM server logs
```

### 3. Run Replay Locally

**Note:** The campaign runner automatically fixes empty header.yaml files during download (no manual fix needed).

```bash
# Single experiment
python blis_orc_scripts/replay.py --experiment-ids 68

# Multiple experiments
python blis_orc_scripts/replay.py --experiment-ids 68,69,70

# With custom BLIS repo location
python blis_orc_scripts/replay.py --experiment-ids 68 --blis-repo ~/inference-sim

# With model config folder (if using HF models)
python blis_orc_scripts/replay.py --experiment-ids 68 --model-config-folder ~/.cache/huggingface/models
```

**What it does:**
- Clones and builds BLIS if needed
- Finds downloaded observe data in campaign folder
- Runs simulation using trained-physics latency model
- Outputs: `blis-campaign/campaign/<exp-dir>/data/<exp-id>/replay/sim_result.json`

### 4. Run Calibrate Locally

```bash
# Single experiment
python blis_orc_scripts/calibrate.py --experiment-ids 68

# Multiple experiments
python blis_orc_scripts/calibrate.py --experiment-ids 68,69,70
```

**What it does:**
- Uses BLIS from step 3
- Compares real trace vs simulated results
- Computes calibration metrics (MAPE, Pearson R, etc.)
- Outputs: `blis-campaign/campaign/<exp-dir>/data/<exp-id>/calibrate/calibration_report.json`

## Directory Structure

After running all steps:

```
blis-campaign/campaign/68-llama-3-1-8b-h100-general-lite/
└── data/
    └── 68-llama-3-1-8b-tp1-general-lite-1-1/
        ├── observe/
        │   ├── header.yaml         # Trace metadata (from cluster)
        │   ├── data.csv            # Request trace (from cluster)
        │   └── workload.yaml       # Workload spec (from cluster)
        ├── replay/
        │   ├── sim_result.json     # Simulation output (local)
        │   ├── stdout.log          # Replay logs (local)
        │   └── stderr.log          # Replay errors (local)
        ├── calibrate/
        │   ├── calibration_report.json  # Calibration metrics (local)
        │   ├── stdout.log               # Calibrate logs (local)
        │   └── stderr.log               # Calibrate errors (local)
        ├── exp-config.yaml         # Experiment configuration (from cluster)
        ├── vllm_logging.json       # vLLM logging config (from cluster)
        └── vllm.log                # vLLM server logs (from cluster)
```

## Script Options

### replay.py

```bash
python blis_orc_scripts/replay.py \
  --experiment-ids 68,69,70 \
  --campaign blis-campaign/campaign \
  --blis-repo ../inference-sim \
  --model-config-folder ~/.cache/huggingface
```

- `--experiment-ids`: Required. Comma-separated experiment IDs
- `--campaign`: Campaign directory (default: `blis-campaign/campaign`)
- `--blis-repo`: Path to inference-sim repo (default: `../inference-sim`, will clone if missing)
- `--model-config-folder`: Optional. Path to HF model configs (uses BLIS bundled configs if not specified)

### calibrate.py

```bash
python blis_orc_scripts/calibrate.py \
  --experiment-ids 68,69,70 \
  --campaign blis-campaign/campaign \
  --blis-repo ../inference-sim
```

- `--experiment-ids`: Required. Comma-separated experiment IDs
- `--campaign`: Campaign directory (default: `blis-campaign/campaign`)
- `--blis-repo`: Path to inference-sim repo (default: `../inference-sim`)

## Troubleshooting

### "Downloaded observe data not found"
The campaign runner automatically downloads data after observe completes. If download failed, check campaign logs or use manual download command above.

### "Replay sim_result.json not found"
Run replay first before calibrate:
```bash
python blis_orc_scripts/replay.py --experiment-ids 68
python blis_orc_scripts/calibrate.py --experiment-ids 68
```

### "BLIS binary not found after build"
Install Go:
```bash
brew install go  # macOS
# or
sudo apt install golang-go  # Ubuntu
```

### Replay fails with "hardware config not found"
The script automatically uses bundled configs from the cloned repo. If it fails, ensure the BLIS repo was cloned successfully:
```bash
ls ../inference-sim/hardware_config.json
ls ../inference-sim/model_configs/
```

### "Empty header.yaml" or "parsing trace header: EOF"
The campaign runner now automatically fixes empty header.yaml files during download. If you manually downloaded data and see this error, create a minimal valid header:
```bash
echo '{}' > blis-campaign/campaign/<exp-dir>/data/<exp-id>/observe/header.yaml
```

## Advantages of Local Replay/Calibrate

1. **No GPU needed** - CPU-only simulation
2. **Fast iteration** - Re-run calibration with different parameters
3. **No cluster quota** - Doesn't consume cluster resources
4. **Easy debugging** - Full logs available locally
5. **Batch processing** - Process multiple experiments in parallel

## Example: Full Workflow

```bash
# 1. Launch observe on cluster (run-campaign.sh handles observe + automatic download)
./blis-campaign/run-campaign.sh --campaign blis-campaign/campaign/ --hw H100 --only 68

# 2. Wait for completion (run-campaign.sh automatically downloads and fixes observe data)

# 3. Run replay and calibrate locally
python blis_orc_scripts/replay.py --experiment-ids 68
python blis_orc_scripts/calibrate.py --experiment-ids 68

# 4. View results
cat blis-campaign/campaign/68-llama-3-1-8b-h100-general-lite/data/68-llama-3-1-8b-tp1-general-lite-1-1/calibrate/calibration_report.json
```
