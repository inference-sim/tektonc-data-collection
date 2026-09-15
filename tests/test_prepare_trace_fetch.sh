#!/bin/sh
# Tests the layout-agnostic corpus fetch in the prepare-trace task
# (inference-sim/tektonc-data-collection#67):
#   - Part 1: structural — the synthesized-filename fetch is gone, the step can
#     actually run what it now runs (python image), and the revision param exists
#   - Part 2: behavioral — the embedded Python's discover -> cap -> fetch-missing
#     logic, exercised against a FAKE huggingface_hub so no network is touched.
#     This is what pins the two properties the issue is about: files are
#     discovered rather than named, and the corpus dir is keyed by repo+revision.
#
# Run with: sh tektonc-data-collection/tests/test_prepare_trace_fetch.sh

PASS=true
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; PASS=false; }

HERE="$(dirname "$0")"
TASK="${HERE}/../tekton/tasks/prepare-trace.yaml"
EXTRACT="${HERE}/lib/extract_step.py"

[ -f "${TASK}" ] || { echo "FAIL: cannot find ${TASK}"; exit 1; }

python3 -c 'import yaml' 2>/dev/null || {
  echo "SKIP: PyYAML not available (pip install -r tektonc/requirements.txt)"
  exit 0
}

STEP="$(python3 "${EXTRACT}" "${TASK}" download-corpus)" || {
  echo "FAIL: could not extract the download-corpus step"; exit 1; }

# ────────────────────────────────────────────────────────────
# Part 1 — structural
# ────────────────────────────────────────────────────────────

# The defect: shard filenames were synthesized from a printf format, which
# only matches HF's sharded-split layout and 404s on split-as-directory.
echo "${STEP}" | grep -q 'of-${TOTAL}' \
  && fail "download-corpus still synthesizes 'train-N-of-TOTAL' filenames" \
  || pass "no synthesized shard filenames remain"

echo "${STEP}" | grep -q '/resolve/main/' \
  && fail "download-corpus still hardcodes the /resolve/main/ URL" \
  || pass "hardcoded /resolve/main/ URL is gone"

# The step now needs python+pip; curlimages/curl has neither and runs non-root.
IMAGE="$(python3 - "${TASK}" <<'PY'
import sys, yaml
task = yaml.safe_load(open(sys.argv[1]))
for s in task["spec"]["steps"]:
    if s["name"] == "download-corpus":
        print(s["image"])
PY
)"
case "${IMAGE}" in
  python:3.11-slim) pass "download-corpus image is ${IMAGE}" ;;
  *) fail "download-corpus image is '${IMAGE}'; needs python+pip to run hf download" ;;
esac

# Pinned because local_dir= must write real files, not cache symlinks. The step
# uses the Python API only, so this is NOT an `hf` CLI floor.
echo "${STEP}" | grep -q 'huggingface_hub>=0.34' \
  && pass "huggingface_hub is pinned (local_dir real-file semantics)" \
  || fail "huggingface_hub is not pinned to >=0.34"

echo "${STEP}" | grep -q 'params.traceRevision' \
  && pass "download-corpus reads the traceRevision param" \
  || fail "download-corpus does not read traceRevision"

# The corpus-dir contract build-otel depends on.
echo "${STEP}" | grep -q '/workspace/corpus_dir' \
  && pass "download-corpus publishes the resolved corpus dir" \
  || fail "download-corpus does not write /workspace/corpus_dir"

# Param declared, defaulting to "" (= repo default branch).
if python3 - "${TASK}" <<'PY'
import sys, yaml
task = yaml.safe_load(open(sys.argv[1]))
p = {x["name"]: x for x in task["spec"]["params"]}
r = p.get("traceRevision")
sys.exit(0 if r and r.get("type") == "string" and r.get("default") == "" else 1)
PY
then pass "traceRevision param declared with default \"\""
else fail "traceRevision param missing or wrong default"
fi

# traceShards MUST survive: sim2real's pipeline.yaml passes it, and Tekton
# rejects a Pipeline passing a param the Task does not declare.
if python3 - "${TASK}" <<'PY'
import sys, yaml
task = yaml.safe_load(open(sys.argv[1]))
sys.exit(0 if any(p["name"] == "traceShards" for p in task["spec"]["params"]) else 1)
PY
then pass "traceShards param still declared (Pipeline contract)"
else fail "traceShards was removed — breaks sim2real pipeline.yaml"
fi

# ────────────────────────────────────────────────────────────
# Part 2 — behavioral: run the embedded Python against a FAKE
# huggingface_hub. Pins the two properties the issue is about, plus the
# cross-dataset collision that motivated keying the corpus dir.
# ────────────────────────────────────────────────────────────
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

# Pull just the heredoc'd Python out of the step script. This MUST succeed:
# if it silently produced an empty program, every behavioral assertion below
# would "pass" against a no-op (0 downloads, empty corpus dir compared to
# empty corpus dir), masking exactly the regression this file exists to catch.
if ! python3 - "${TASK}" > "${TMP}/download.py" <<'PY'
import sys, yaml
task = yaml.safe_load(open(sys.argv[1]))
step = next(s for s in task["spec"]["steps"] if s["name"] == "download-corpus")
lines = step["script"].splitlines()
try:
    start = next(i for i, l in enumerate(lines) if l.strip().endswith("<<'PYEOF'")) + 1
    end = next(i for i, l in enumerate(lines) if l.strip() == "PYEOF")
except StopIteration:
    sys.exit("download-corpus has no <<'PYEOF' ... PYEOF python program")
body = lines[start:end]
indent = min((len(l) - len(l.lstrip()) for l in body if l.strip()), default=0)
print("\n".join(l[indent:] for l in body))
PY
then
  fail "could not extract an embedded python program from download-corpus"
  echo
  echo "FAILURES"
  exit 1
fi
[ -s "${TMP}/download.py" ] || {
  fail "extracted download-corpus program is empty"
  echo; echo "FAILURES"; exit 1; }

# Fake huggingface_hub: two datasets with the two real layouts, and a
# hf_hub_download that just touches the file under local_dir.
mkdir -p "${TMP}/fake"
cat > "${TMP}/fake/huggingface_hub.py" <<'PY'
import os

LAYOUTS = {
    # sharded-split (v1) and split-as-directory (v2) — the two real shapes
    "Exgentic/agent-llm-traces": (
        "sha_v1",
        ["data/train-%05d-of-00039.parquet" % i for i in range(39)],
    ),
    "Exgentic/agent-llm-traces-v2": (
        "sha_v2",
        ["data/train/%04d.parquet" % i for i in range(9)],
    ),
}


class _Info:
    def __init__(self, sha):
        self.sha = sha


class HfApi:
    def dataset_info(self, repo, revision=None):
        sha, _ = LAYOUTS[repo]
        # A pinned revision resolves to itself; a moving ref to the head sha.
        return _Info(revision if revision and revision.startswith("sha_") else sha)

    def list_repo_files(self, repo, repo_type=None, revision=None):
        return list(LAYOUTS[repo][1])


def hf_hub_download(repo, filename=None, repo_type=None, revision=None,
                    local_dir=None):
    dest = os.path.join(local_dir, filename)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w") as fh:
        fh.write("stub")
    with open(os.environ["DL_LOG"], "a") as fh:
        fh.write(dest + "\n")
    return dest
PY

# Redirect the step's absolute /workspace paths into the sandbox.
sed -i.bak "s#/workspace#${TMP}/workspace#g" "${TMP}/download.py"
mkdir -p "${TMP}/workspace/data"

# run_dl <repo> <rev> <shards> -> resolves the corpus dir; the files it
# downloaded land in ${TMP}/dl.log (truncated per run).
run_dl() {
  : > "${TMP}/dl.log"
  DL_LOG="${TMP}/dl.log" \
  PYTHONPATH="${TMP}/fake" \
  REPO="$1" REV="$2" SHARDS="$3" \
  python3 "${TMP}/download.py" > "${TMP}/out.txt" 2>&1
  rc=$?
  # QUIET=1 for cases that EXPECT a non-zero exit, so a passing test
  # does not print a scary "step output" dump.
  [ ${rc} -eq 0 ] || [ "${QUIET}" = "1" ] || { echo "--- step output ---"; cat "${TMP}/out.txt"; }
  return ${rc}
}

# (a) split-as-directory (v2) — the layout that used to 404 outright.
if run_dl "hf:Exgentic/agent-llm-traces-v2" "" ""; then
  n=$(wc -l < "${TMP}/dl.log" | tr -d ' ')
  [ "${n}" = "9" ] \
    && pass "v2 split-as-directory: discovered and fetched all 9 files" \
    || fail "v2: expected 9 downloads, got ${n}"
  grep -q 'data/train/0000.parquet' "${TMP}/dl.log" \
    && pass "v2: nested path preserved under the corpus dir" \
    || fail "v2: nested data/train/ path not preserved"
  V2_DIR="$(cat "${TMP}/workspace/corpus_dir")"
else
  fail "v2 fetch raised"
  V2_DIR=""
fi

# (b) sharded-split (v1) still works, and lands in a DIFFERENT directory.
if run_dl "hf:Exgentic/agent-llm-traces" "" ""; then
  n=$(wc -l < "${TMP}/dl.log" | tr -d ' ')
  [ "${n}" = "39" ] \
    && pass "v1 sharded-split: discovered and fetched all 39 files" \
    || fail "v1: expected 39 downloads, got ${n}"
  V1_DIR="$(cat "${TMP}/workspace/corpus_dir")"
else
  fail "v1 fetch raised"
  V1_DIR=""
fi

if [ -n "${V1_DIR}" ] && [ -n "${V2_DIR}" ]; then
  [ "${V1_DIR}" != "${V2_DIR}" ] \
    && pass "the two datasets resolve to different corpus dirs" \
    || fail "COLLISION: both datasets share corpus dir ${V1_DIR}"
fi

# (c) THE BUG: with a shared dir, v1's 39 files made a 9-shard v2 request
# skip its download (39 >= 9) and convert v1 data. Re-running v2 now must
# still resolve to v2's own dir and never see v1's files.
if run_dl "hf:Exgentic/agent-llm-traces-v2" "" ""; then
  n=$(wc -l < "${TMP}/dl.log" | tr -d ' ')
  [ "${n}" = "0" ] \
    && pass "v2 re-run is a no-op (per-file idempotency, not a count guard)" \
    || fail "v2 re-run re-downloaded ${n} file(s)"
  [ "$(cat "${TMP}/workspace/corpus_dir")" = "${V2_DIR}" ] \
    && pass "v2 re-run resolves to its own dir, unaffected by v1's 39 files" \
    || fail "v2 re-run resolved elsewhere after v1 populated the cache"
else
  fail "v2 re-run raised"
fi

# (d) a pinned revision is honoured and keys a separate dir.
if run_dl "hf:Exgentic/agent-llm-traces-v2" "sha_pinned" ""; then
  PINNED_DIR="$(cat "${TMP}/workspace/corpus_dir")"
  case "${PINNED_DIR}" in
    *@sha_pinned) pass "pinned traceRevision keys the corpus dir" ;;
    *) fail "pinned revision not reflected in corpus dir: ${PINNED_DIR}" ;;
  esac
  [ "${PINNED_DIR}" != "${V2_DIR}" ] \
    && pass "pinned revision does not reuse the moving-ref corpus" \
    || fail "pinned revision collided with the moving-ref corpus"
else
  fail "pinned-revision fetch raised"
fi

# (e) the deprecated shard cap still truncates the DISCOVERED list. Pinned to
# a fresh revision so the corpus dir is empty — against the already-populated
# default-revision dir this would correctly fetch nothing, which would test
# idempotency rather than the cap.
if run_dl "hf:Exgentic/agent-llm-traces" "sha_capped" "3"; then
  n=$(wc -l < "${TMP}/dl.log" | tr -d ' ')
  [ "${n}" = "3" ] \
    && pass "traceShards=3 fetches only the first 3 of 39 discovered files" \
    || fail "traceShards=3 fetched ${n} file(s)"
  grep -q 'discovered=39 selected=3' "${TMP}/out.txt" \
    && pass "the cap is applied to the discovered list, not to a synthesized count" \
    || { fail "expected 'discovered=39 selected=3' in the step log"; cat "${TMP}/out.txt"; }
  # The cap must take a PREFIX of the sorted list (the documented, if
  # benchmark-biased, semantics the old shard loop had).
  grep -q 'train-00000-of-00039.parquet' "${TMP}/dl.log" \
    && ! grep -q 'train-00003-of-00039.parquet' "${TMP}/dl.log" \
    && pass "the cap takes the sorted prefix" \
    || fail "the cap did not take the sorted prefix"
else
  fail "shard-capped fetch raised"
fi

# (f) an empty traceSource fails loudly rather than fetching nothing quietly.
if QUIET=1 run_dl "" "" ""; then
  fail "empty traceSource was accepted"
else
  grep -q 'traceSource is empty' "${TMP}/out.txt" \
    && pass "empty traceSource fails with a clear message" \
    || { fail "empty traceSource failed without the expected message"; cat "${TMP}/out.txt"; }
fi

echo
if ${PASS}; then echo "ALL PASS"; exit 0; else echo "FAILURES"; exit 1; fi
