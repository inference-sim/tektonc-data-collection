"""Experiment YAML generator for BLIS campaign.

Reads experiments.json + config files, builds per-experiment values.yaml
overrides, calls tektonc for pipeline compilation, and generates pipelinerun.yaml.
"""
import copy
import json
import re
import subprocess
import sys
import yaml
from pathlib import Path

# Import combine_workload for dynamic workload generation
sys.path.insert(0, str(Path(__file__).parent))
from combine_workload import combine_workload


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_yaml(path):
    """Load YAML file."""
    with open(path) as f:
        return yaml.safe_load(f)


def write_yaml(path, data):
    """Write data to YAML file."""
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, width=200)


def write_json(path, data):
    """Write data to JSON file."""
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_experiments(path):
    """Load and return experiments list from JSON."""
    with open(path) as f:
        return json.load(f)


def load_clusters(path):
    """Load clusters.yaml config."""
    return load_yaml(path)


def generate_workload_for_experiment(exp, patterns_file):
    """
    Generate workload dynamically for an experiment.

    Args:
        exp: Experiment dict with 'workload' and 'arrival_pattern' fields
        patterns_file: Path to arrival-and-workload-patterns.yaml

    Returns:
        dict: BLIS native workload structure

    Raises:
        ValueError: If workload generation fails
    """
    try:
        workload_data = combine_workload(
            patterns_file=patterns_file,
            workload_name=exp["workload"],
            arrival_pattern=exp["arrival_pattern"],
            output_file=None,  # Don't write to file
            seed=42
        )
        return workload_data
    except ValueError as e:
        raise ValueError(f"Experiment #{exp['id']}: {e}")
    except Exception as e:
        raise RuntimeError(f"Experiment #{exp['id']}: Failed to generate workload: {e}")


def build_inference_perf_orcspec(exp, static_workloads):
    """Build an ORC orcSpec from a workloads.yaml inference_perf profile.

    The ORC pipeline accepts inference_perf workloads inline (see the default
    orcSpec in tektoncsample/blis-orc/values.yaml); the BLIS binary translates
    them to a trace during observe (orc-observe.yaml). This reshapes a
    workloads.yaml entry {load.stages, data.shared_prefix} into that orcSpec
    shape verbatim -- no cohort/distribution synthesis needed.

    Args:
        exp: Experiment dict (its 'workload' names a workloads.yaml profile)
        static_workloads: Parsed workloads.yaml (name -> profile)

    Returns:
        dict: {"inference_perf": {"stages": [...], "shared_prefix": {...}}}
    """
    wl = static_workloads[exp["workload"]]  # presence guaranteed by validate_all
    data = wl.get("data", {})
    data_type = data.get("type")
    if data_type != "shared_prefix":
        raise ValueError(
            f"Experiment #{exp['id']}: workload '{exp['workload']}' has "
            f"data.type='{data_type}'; only 'shared_prefix' is supported for "
            f"inference_perf translation."
        )
    return {
        "inference_perf": {
            "stages": wl["load"]["stages"],
            "shared_prefix": data["shared_prefix"],
        }
    }


def build_blis_native_orcspec(exp, static_workloads):
    """Return a blis_native profile's WorkloadSpec verbatim as the orcSpec.

    A blis_native profile's `blis:` block is already a complete BLIS WorkloadSpec
    (version/seed/clients/num_requests/aggregate_rate/...). orc-observe.yaml writes
    whatever dict lands at values.yaml -> workload.orcSpec straight to workload.yaml
    and feeds it to `blis observe --workload-spec`, which parses it strictly. So no
    reshaping is needed -- pass the block through as-is.

    Args:
        exp: Experiment dict (its 'workload' names a workloads.yaml profile)
        static_workloads: Parsed workloads.yaml (name -> profile)

    Returns:
        dict: the profile's `blis` block (a top-level WorkloadSpec)
    """
    wl = static_workloads[exp["workload"]]  # presence guaranteed by validate_all
    return wl["blis"]


# ---------------------------------------------------------------------------
# Validation (collect all errors, don't fail on first)
# ---------------------------------------------------------------------------

def validate_all(experiments, clusters, patterns_data, static_workloads=None):
    """
    Validate all experiments. Returns list of error strings (empty = valid).

    Args:
        patterns_data: Dict from arrival-and-workload-patterns.yaml with
                      'arrival_patterns' and 'workloads' keys
        static_workloads: Dict from workloads.yaml (name -> profile). Workloads
                      here are inference_perf profiles for which arrival_pattern
                      is not applicable (load comes from load.stages).
    """
    errors = []
    valid_hw = {k for k in clusters if k != "namespace"}
    valid_harnesses = {"inference-perf", "orc", "blis-orc"}

    # Extract arrival patterns and workloads from patterns_data
    arrival_patterns = patterns_data.get("arrival_patterns", {})
    workloads = patterns_data.get("workloads", {})
    static_workloads = static_workloads or {}

    for exp in experiments:
        eid = exp.get("id", "?")

        # Model ID validation (must look like a HuggingFace ID with /)
        if "/" not in exp["model"]:
            errors.append(f"Experiment #{eid}: model '{exp['model']}' must be a full HuggingFace ID (org/model)")
        if exp["hw"] not in valid_hw:
            errors.append(f"Experiment #{eid}: unknown hw '{exp['hw']}'")

        # Validate workload exists in one of the two sources:
        #  - patterns file: statistical/dynamic workloads (need arrival_pattern)
        #  - workloads.yaml: inference_perf profiles (arrival_pattern N/A)
        is_dynamic = exp["workload"] in workloads
        is_static = exp["workload"] in static_workloads
        if not is_dynamic and not is_static:
            errors.append(f"Experiment #{eid}: unknown workload '{exp['workload']}'")

        # arrival_pattern is only required/validated for dynamic (patterns) workloads.
        # For inference_perf workloads it is ignored (load comes from load.stages).
        if is_dynamic:
            if "arrival_pattern" not in exp:
                errors.append(f"Experiment #{eid}: missing 'arrival_pattern' field")
            elif exp["arrival_pattern"] not in arrival_patterns:
                errors.append(f"Experiment #{eid}: unknown arrival_pattern '{exp['arrival_pattern']}'")

        # Validate harness
        harness = exp.get("harness", "inference-perf")
        if harness not in valid_harnesses:
            errors.append(f"Experiment #{eid}: unknown harness '{harness}' (valid: {valid_harnesses})")

        # Validate saturation_detectors (optional). Mirrors BLIS rules:
        # must be a list of strings; "all" only alone; other names must be valid.
        # Saturation is an observe-phase feature -> ORC harness only.
        if "saturation_detectors" in exp:
            dets = exp["saturation_detectors"]
            if not isinstance(dets, list) or not all(isinstance(d, str) for d in dets):
                errors.append(
                    f"Experiment #{eid}: 'saturation_detectors' must be a list of strings"
                )
            elif dets:  # non-empty
                if harness not in ["orc", "blis-orc"]:
                    errors.append(
                        f"Experiment #{eid}: 'saturation_detectors' requires harness "
                        f"'orc'/'blis-orc' (saturation is an observe-phase feature)"
                    )
                if "all" in dets and len(dets) > 1:
                    errors.append(
                        f"Experiment #{eid}: 'saturation_detectors' \"all\" cannot be "
                        f"combined with individual detector names"
                    )
                bad = [d for d in dets if d != "all" and d not in VALID_SATURATION_DETECTORS]
                if bad:
                    valid = ", ".join(sorted(VALID_SATURATION_DETECTORS)) + ', or "all"'
                    errors.append(
                        f"Experiment #{eid}: unknown saturation detector(s) {bad}; "
                        f"valid: {valid}"
                    )

        # Validate combination is valid (dynamic workload has data for arrival_pattern)
        if is_dynamic and "arrival_pattern" in exp:
            wl = workloads[exp["workload"]]
            if exp["arrival_pattern"] not in wl:
                available = ", ".join(wl.keys())
                errors.append(
                    f"Experiment #{eid}: workload '{exp['workload']}' does not have "
                    f"data for arrival_pattern '{exp['arrival_pattern']}'. "
                    f"Available: {available}"
                )

        # Static workloads must be spec: inference_perf or blis_native.
        #  - inference_perf: rate/duration stages + shared_prefix (translated by BLIS)
        #  - blis_native: a full BLIS WorkloadSpec under a `blis:` block (passed through
        #    verbatim as orcSpec). blis_native is ORC-only (needs the observe harness).
        if is_static and not is_dynamic:
            profile = static_workloads[exp["workload"]]
            spec = profile.get("spec")
            if spec not in ("inference_perf", "blis_native"):
                errors.append(
                    f"Experiment #{eid}: workload '{exp['workload']}' in workloads.yaml "
                    f"has spec='{spec}'; only 'inference_perf' and 'blis_native' profiles "
                    f"are supported."
                )
            elif spec == "blis_native":
                if "blis" not in profile:
                    errors.append(
                        f"Experiment #{eid}: workload '{exp['workload']}' has "
                        f"spec='blis_native' but no 'blis:' block."
                    )
                if harness not in ("orc", "blis-orc"):
                    errors.append(
                        f"Experiment #{eid}: workload '{exp['workload']}' is blis_native, "
                        f"which requires harness='orc'/'blis-orc' (got '{harness}')."
                    )

        # Dynamic workloads require ORC harness
        if harness not in ["orc", "blis-orc"]:
            errors.append(
                f"Experiment #{eid}: dynamically generated workloads require "
                f"harness='orc' or 'blis-orc', got harness='{harness}'"
            )

    return errors


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def make_dns_name(s, max_len=63):
    """Convert string to DNS-1123 compatible name.

    max_len defaults to the DNS-1123 label limit (63). Callers that feed the
    result into stricter contexts (e.g. Helm release names, capped at 53) pass
    a smaller max_len. Truncation strips any trailing dash so the result stays
    a valid DNS-1123 name.
    """
    s = s.lower()
    s = re.sub(r'[^a-z0-9-]', '-', s)
    s = re.sub(r'-+', '-', s)
    s = s.strip('-')
    if len(s) > max_len:
        s = s[:max_len].rstrip('-')
    return s


def make_dir_name(exp):
    """e.g. '13-qwen3-14b-h100-general-afternoon'"""
    base = f"{exp['id']}-{exp['model']}-{exp['hw']}-{exp['workload']}"
    arrival = exp.get('arrival_pattern', '')
    if arrival:
        return make_dns_name(f"{base}-{arrival}")
    return make_dns_name(base)


# Helm caps release names at 53 chars. The deploy-model release name is
# "{experimentId}-{tp}-{dlp}-model" (modelLabel = experimentId-tp-dlp), so the
# experimentId itself must leave room for that "-{tp}-{dlp}-model" suffix.
HELM_RELEASE_MAX = 53


def make_experiment_id(exp):
    """e.g. '13-qwen3-14b-tp1-general' -- used as PVC data path and Helm label.

    Bounded so "{experimentId}-{tp}-{dlp}-model" (the deploy-model Helm release
    name) stays within Helm's 53-char limit. Long full-HF model ids (e.g.
    'nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4') would otherwise blow
    past it; short ids are unaffected.
    """
    tp = exp["tp"]
    dlp = exp.get("dp") or 1
    # Reserve the exact suffix the template/chart append after the experiment id.
    suffix_len = len(f"-{tp}-{dlp}-model")
    max_len = HELM_RELEASE_MAX - suffix_len
    return make_dns_name(
        f"{exp['id']}-{exp['model']}-tp{tp}-{exp['workload']}",
        max_len=max_len,
    )


# resolve_model() removed - experiments.json now contains full HuggingFace IDs directly


def extract_pipeline_name(pipeline_yaml_path):
    """Read compiled pipeline.yaml and extract metadata.name."""
    data = load_yaml(pipeline_yaml_path)
    return data["metadata"]["name"]


# ---------------------------------------------------------------------------
# Values builder (the core logic)
# ---------------------------------------------------------------------------

DEFAULT_KV_OFFLOAD_GB = 8.0

# vLLM v0.26 CPU KV-cache offloading (OffloadingConnector) defaults.
# The connector takes an absolute byte budget for the CPU pool, unlike the
# legacy --kv-offloading-size flag which took GiB. Values match the manifest
# validated in kv-offload-cpu.yaml.
KV_OFFLOAD_CPU_BYTES = 10 * 1024**3  # 10 GiB
KV_OFFLOAD_BLOCK_SIZE = 16
KV_OFFLOAD_EVICTION_POLICY = "lru"

# Valid BLIS saturation detectors (source: inference-sim sim/saturation/bank.go
# rosterOrder; surfaced by `blis observe --detectors`). "all" is a keyword that
# expands to the whole roster and cannot be combined with individual names.
VALID_SATURATION_DETECTORS = {"composite", "threshold", "backlog-drift"}

MOE_MODELS = {"Mixtral-8x7B", "DeepSeek-V3", "Llama-4-Scout-17B-16E"}


def build_values(exp, base_values, clusters, patterns_file, patterns_data=None, static_workloads=None):
    """
    Build per-experiment values.yaml from base template + experiment config.

    Args:
        exp: Experiment dict
        base_values: Base values template
        clusters: Clusters config dict
        patterns_file: Path to arrival-and-workload-patterns.yaml
        patterns_data: Parsed patterns file (to decide the workload source)
        static_workloads: Parsed workloads.yaml (inference_perf profiles)
    """
    patterns_data = patterns_data or {}
    static_workloads = static_workloads or {}
    v = copy.deepcopy(base_values)

    # Use model ID directly from experiments.json (full HuggingFace ID)
    hf_id = exp["model"]

    # Experiment identity
    exp_name = make_dns_name(f"blis-{exp['id']}-{exp['model']}-{exp['workload']}")
    v["experiment"]["name"] = exp_name
    v["experiment"]["description"] = (
        f"Exp #{exp['id']}: {exp['model']} {exp['precision']} "
        f"TP{exp['tp']} {exp['workload']} on {exp['hw']}"
    )

    # Stack config
    v["stack"]["MAX_NUM_BATCHED_TOKENS"] = exp["mbt"]
    # Allow experiments.json to override MAX_MODEL_LEN (default from base values if not specified)
    if "max_model_len" in exp:
        v["stack"]["MAX_MODEL_LEN"] = exp["max_model_len"]
    # Allow experiments.json to override MAX_NUM_SEQS (default from base values if not specified)
    if "max_num_seqs" in exp:
        v["stack"]["MAX_NUM_SEQS"] = exp["max_num_seqs"]
    # Allow experiments.json to override BLOCK_SIZE (default from base values if not specified)
    if "block_size" in exp:
        v["stack"]["BLOCK_SIZE"] = exp["block_size"]
    v["stack"]["treatments"]["tensorParallelism"] = [exp["tp"]]
    v["stack"]["treatments"]["dataLocalParallelism"] = [exp.get("dp") or 1]

    # Store kv_offload flag and offloading size for template access.
    # NOTE: experiments.json uses the key "kv_offload"; older code read
    # "cpu_offload" (which never existed in the data), so offloading never
    # actually triggered. kv_offloading_size is informational only (recorded
    # in exp-config.yaml); the real vLLM config is built in build_extra_overrides.
    v["stack"]["kv_offload"] = exp.get("kv_offload", False)
    v["stack"]["kv_offloading_size"] = DEFAULT_KV_OFFLOAD_GB if exp.get("kv_offload") else 0

    # GPU targeting
    cluster = clusters[exp["hw"]]
    v["stack"]["model"]["helmValues"]["decode"]["acceleratorTypes"]["labelValues"] = [
        cluster["gpu_label_value"]
    ]

    # GPU reaper exclusion — prevent reaper from killing experiment deployments
    decode = v["stack"]["model"]["helmValues"]["decode"]
    if "annotations" not in decode:
        decode["annotations"] = {}
    decode["annotations"]["gpu-reaper.io/exclude"] = "true"

    # Build extra_overrides (handles ALL capacity-related vLLM args including CPU offloading)
    # Observability template only handles observability features, NOT capacity management
    v["stack"]["extra_overrides"] = build_extra_overrides(exp)

    # Workload profile - two possible sources:
    #  (a) patterns file  -> statistical BLIS-native cohorts (needs arrival_pattern)
    #  (b) workloads.yaml  -> inference_perf profile, passed through for BLIS to translate
    patterns_workloads = patterns_data.get("workloads", {})
    if exp["workload"] in patterns_workloads:
        wl = generate_workload_for_experiment(exp, patterns_file)
    elif static_workloads.get(exp["workload"], {}).get("spec") == "blis_native":
        wl = build_blis_native_orcspec(exp, static_workloads)
    else:
        wl = build_inference_perf_orcspec(exp, static_workloads)
    harness = exp.get("harness", "inference-perf")  # Default to inference-perf for backward compatibility

    # Both sources emit an orcSpec the ORC pipeline understands
    if harness in ["orc", "blis-orc"]:
        v["workload"]["orcSpec"] = wl

        # Set horizon to 25 minutes (1500 seconds) for ORC experiments
        # This is the total simulation time to let requests complete
        # (spike duration is 600s for request generation; horizon gives 15 more minutes for completion)
        v["workload"]["horizon"] = 1500

        # Saturation detection (optional). A non-empty saturation_detectors list
        # enables detection and selects which BLIS detector(s) to run; absent/empty
        # = off. Joined into the comma-string `blis observe --detectors` expects
        # (empty string = off, matching BLIS's own --detectors semantics).
        detectors = exp.get("saturation_detectors") or []
        v["workload"]["saturationDetectors"] = ",".join(detectors)
    else:
        raise ValueError(
            f"Dynamically generated workloads use BLIS native format "
            f"and require harness='orc' or 'blis-orc'. "
            f"Experiment #{exp['id']} has harness='{harness}'"
        )

    return v


def build_extra_overrides(exp):
    """Build the list of Helm override strings for extra vLLM args.

    This handles ALL capacity-related vLLM configuration including CPU offloading.
    Observability templates handle ONLY observability features (tracing, KV events).
    """
    overrides = []

    # vLLM image version (if explicitly specified in experiments.json)
    if "vllm_version" in exp:
        vllm_version = exp["vllm_version"]
        overrides.append(f'decode.containers[name="vllm"].image=vllm/vllm-openai:v{vllm_version}')

    # FP8 quantization
    if exp.get("precision") == "FP8":
        overrides.append(
            'decode.containers[name="vllm"].args=--quantization=fp8'
        )

    # GPU memory utilization (only if non-default)
    if exp["gpu_mem"] != 0.9:
        overrides.append(
            f'decode.containers[name="vllm"].args=--gpu-memory-utilization={exp["gpu_mem"]}'
        )

    # CPU KV cache offloading - CAPACITY MANAGEMENT (vLLM v0.26 style).
    # Uses the OffloadingConnector / CPUOffloadingSpec kv-transfer-config, matching
    # the config validated in kv-offload-cpu.yaml. Evicted GPU KV blocks spill to a
    # CPU RAM pool instead of being dropped, extending effective cache capacity.
    # The legacy --kv-offloading-size / --disable-hybrid-kv-cache-manager flags
    # (v0.15/v0.19 era) are no longer used; the connector supersedes them.
    # NOTE: independent of observability - observability can run with or without offloading.
    if exp.get("kv_offload"):
        kv_transfer_config = json.dumps(
            {
                "kv_connector": "OffloadingConnector",
                "kv_role": "kv_both",
                "kv_connector_extra_config": {
                    "spec_name": "CPUOffloadingSpec",
                    "cpu_bytes_to_use": KV_OFFLOAD_CPU_BYTES,
                    "block_size": KV_OFFLOAD_BLOCK_SIZE,
                    "eviction_policy": KV_OFFLOAD_EVICTION_POLICY,
                },
            },
            separators=(",", ":"),  # compact, single-line JSON for the Helm --set parser
        )
        overrides.append(
            f'decode.containers[name="vllm"].args=--kv-transfer-config={kv_transfer_config}'
        )

    # Expert parallelism for MoE models with data-local parallelism > 1
    dp = exp.get("dp")
    if dp and dp > 1 and exp["model"] in MOE_MODELS:
        overrides.append(
            'decode.containers[name="vllm"].args=--enable-expert-parallel'
        )

    # Block size (explicitly set if specified in experiments.json)
    if "block_size" in exp:
        overrides.append(f'decode.containers[name="vllm"].args=--block-size={exp["block_size"]}')

    # Prefix caching - only pass flag if explicitly set in experiments.json
    if "enable_prefix_caching" in exp:
        if exp["enable_prefix_caching"]:
            overrides.append('decode.containers[name="vllm"].args=--enable-prefix-caching')
        else:
            overrides.append('decode.containers[name="vllm"].args=--no-enable-prefix-caching')
    # If not set, let vLLM use its default (True in v0.17.1)

    # Chunked prefill - only pass flag if explicitly set to True
    if exp.get("enable_chunked_prefill"):
        overrides.append('decode.containers[name="vllm"].args=--enable-chunked-prefill')
    # If False or not set, omit flag (vLLM defaults to False)

    # Priority scheduling policy - check scheduling field in experiments.json
    if exp.get("scheduling") == "priority":
        overrides.append('decode.containers[name="vllm"].args=--scheduling-policy=priority')
    # If "fcfs" or not set, omit flag (vLLM defaults to fcfs - first come first serve)

    # Per-iteration (step-wise) vLLM logging - DIAGNOSTIC. Emits one log record
    # per engine step (iteration_index, ctx/gen request+token counts, elapsed_ms).
    # High volume (hundreds of lines/sec under load); intended for short
    # diagnostic runs, not full campaigns.
    if exp.get("log_iteration_details"):
        overrides.append('decode.containers[name="vllm"].args=--enable-logging-iteration-details')

    # Generic passthrough for model-specific vLLM flags not covered by the
    # structured fields above (e.g. recipe flags like --mamba-backend,
    # --moe-backend, --reasoning-parser). Each entry is appended verbatim as a
    # vLLM arg. To avoid double-emitting, keep flags that ARE covered above
    # (--max-num-seqs, --max-num-batched-tokens, --enable-prefix-caching,
    # --quantization via precision, etc.) in their structured fields, not here.
    for arg in exp.get("extra_vllm_args", []):
        overrides.append(f'decode.containers[name="vllm"].args={arg}')

    return overrides


# ---------------------------------------------------------------------------
# PipelineRun builder
# ---------------------------------------------------------------------------

PIPELINERUN_TEMPLATE = """\
apiVersion: tekton.dev/v1
kind: PipelineRun
metadata:
  name: __PIPELINE_RUN_NAME__
spec:
  timeouts:
    pipeline: 6h
    tasks: 5h30m
  taskRunTemplate:
    serviceAccountName: helm-installer
  workspaces:
    - name: model-cache
      persistentVolumeClaim:
        claimName: model-pvc
    - name: data
      persistentVolumeClaim:
        claimName: data-pvc
    - name: hf-credentials
      secret:
        secretName: hf-secret
        items:
          - key: HF_TOKEN
            path: HF_TOKEN
    - name: target-credentials
      secret:
        secretName: s3-secret
        items:
          - key: AWS_ACCESS_KEY_ID
            path: AWS_ACCESS_KEY_ID
          - key: AWS_SECRET_ACCESS_KEY
            path: AWS_SECRET_ACCESS_KEY
  pipelineRef:
    name: {pipeline_name}
  params:
    - {{ name: experimentId, value: "{experiment_id}" }}
    - {{ name: model, value: "{hf_model}" }}
    - {{ name: namespace, value: "{namespace}" }}
"""


def build_pipelinerun(pipeline_name, experiment_id, hf_model, namespace):
    """Build pipelinerun.yaml content."""
    return PIPELINERUN_TEMPLATE.format(
        pipeline_name=pipeline_name,
        experiment_id=experiment_id,
        hf_model=hf_model,
        namespace=namespace,
    )


# ---------------------------------------------------------------------------
# Main generate loop
# ---------------------------------------------------------------------------

def generate_campaign(args):
    """Main entry point for 'blis-campaign generate'."""
    config_dir = Path(__file__).parent / "config"
    experiments = load_experiments(args.experiments)
    clusters = load_clusters(config_dir / "clusters.yaml")

    # Load arrival and workload patterns (dynamic/statistical workload source)
    patterns_file = Path(__file__).parent / "arrival-and-workload-patterns.yaml"
    patterns_data = load_yaml(patterns_file)

    # Load workloads.yaml (static inference_perf workload source: general, codegen, ...)
    static_workloads = load_yaml(Path(__file__).parent.parent / "workloads.yaml")

    # Load base values for each harness type
    tektoncsample_base = Path(__file__).parent.parent / "tektoncsample"

    # inference-perf harness base values (stock and observability)
    base_values_inference_perf_stock = load_yaml(
        tektoncsample_base / "blis-inference-perf" / "values.yaml"
    )
    base_values_inference_perf_observability = load_yaml(
        tektoncsample_base / "blis-inference-perf" / "values-observability.yaml"
    )

    # ORC harness base values (stock and observability)
    base_values_orc_stock = load_yaml(
        tektoncsample_base / "blis-orc" / "values.yaml"
    )
    base_values_orc_observability = load_yaml(
        tektoncsample_base / "blis-orc" / "values-observability.yaml"
    )

    # Filter to specific IDs if --only is given
    only_ids = None
    if getattr(args, "only", None):
        only_ids = {int(x.strip()) for x in args.only.split(",")}
        experiments = [e for e in experiments if e["id"] in only_ids]

    # Skip done experiments unless --all or --only
    include_all = getattr(args, "all", False)
    if not include_all and not only_ids:
        experiments = [e for e in experiments if not e.get("done", False)]

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Validate all experiments first (fail-fast)
    errors = validate_all(experiments, clusters, patterns_data, static_workloads)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1

    # Template paths for different harnesses
    tektoncsample_base = Path(__file__).parent.parent / "tektoncsample"
    TEMPLATE_PATHS = {
        "inference-perf": tektoncsample_base / "blis-inference-perf" / "data_pipeline.yaml.j2",
        "orc": tektoncsample_base / "blis-orc" / "data_pipeline.yaml.j2",
    }

    generated = 0
    for exp in experiments:
        # Select template based on harness (default to inference-perf)
        harness = exp.get("harness", "inference-perf")
        if harness not in TEMPLATE_PATHS:
            print(f"ERROR: Unknown harness type '{harness}' for experiment #{exp['id']}", file=sys.stderr)
            return 1
        template_path = TEMPLATE_PATHS[harness]
        dir_name = make_dir_name(exp)
        exp_dir = output_dir / dir_name
        exp_dir.mkdir(parents=True, exist_ok=True)

        # 1. Save experiment.json (copy of this experiment's entry)
        write_json(exp_dir / "experiment.json", exp)

        # 2. Select base values based on harness and observability flag
        harness = exp.get("harness", "inference-perf")
        use_observability = exp.get("observability", False)

        if harness == "orc":
            # ORC harness (supports observability)
            base_values = (base_values_orc_observability if use_observability
                          else base_values_orc_stock)
        else:
            # inference-perf harness (default)
            base_values = (base_values_inference_perf_observability if use_observability
                          else base_values_inference_perf_stock)

        # 3. Build and save values.yaml
        values = build_values(exp, base_values, clusters, patterns_file, patterns_data, static_workloads)
        write_yaml(exp_dir / "values.yaml", values)

        # 4. Compile pipeline.yaml via tektonc
        result = subprocess.run(
            [sys.executable,
             str(Path(__file__).parent.parent / "tektonc/tektonc.py"),
             "-t", str(template_path),
             "-f", str(exp_dir / "values.yaml"),
             "-o", str(exp_dir / "pipeline.yaml")],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"FATAL: tektonc failed for experiment #{exp['id']}:\n{result.stderr}",
                  file=sys.stderr)
            return 1

        # 5. Extract pipeline name and generate pipelinerun.yaml
        pipeline_name = extract_pipeline_name(exp_dir / "pipeline.yaml")
        hf_id = exp["model"]
        experiment_id = make_experiment_id(exp)
        pr_yaml = build_pipelinerun(
            pipeline_name, experiment_id, hf_id, clusters["namespace"]
        )
        (exp_dir / "pipelinerun.yaml").write_text(pr_yaml)

        generated += 1
        obs_marker = " [obs]" if use_observability else ""
        harness_marker = f" [{harness}]" if harness != "inference-perf" else ""
        print(f"  [{generated}/{len(experiments)}] #{exp['id']} {exp['model']} "
              f"{exp['hw']} {exp['workload']}{obs_marker}{harness_marker}")

    print(f"Generated {generated} experiments in {output_dir}/")
    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate BLIS campaign experiments")
    parser.add_argument("--experiments", required=True, help="Path to experiments.json")
    parser.add_argument("--output", required=True, help="Output directory for campaign")
    parser.add_argument("--only", help="Comma-separated list of experiment IDs to generate")
    args = parser.parse_args()
    sys.exit(generate_campaign(args))
