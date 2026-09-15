# prepare-trace Layout-Agnostic Fetch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `prepare-trace` fetch an HF corpus by *discovering* its files rather than synthesizing shard filenames, so it works against any HF dataset layout, keys the on-PVC corpus cache by repo + resolved revision, and reads parquet in bounded memory.

**Architecture:** All changes are confined to `tekton/tasks/prepare-trace.yaml`. The `download-corpus` step is rewritten from a `curl` loop over synthesized `train-NNNNN-of-NNNNN.parquet` names into a `huggingface_hub`-driven discover-then-fetch step that resolves the dataset revision to a commit SHA, derives a per-`(repo, sha)` corpus directory, and downloads only the files that are missing. It publishes the resolved directory to `/workspace/corpus_dir` (the same cross-step marker mechanism the existing `/workspace/skip` guard already uses), which `build-otel` reads instead of hardcoding `/workspace/data/corpus`. `build-otel` additionally globs recursively (`hf` preserves repo structure, so shards land at `<corpus>/data/train/0000.parquet`) and streams each shard via `ParquetFile.iter_batches` with column projection.

**Tech Stack:** Tekton Task YAML, POSIX `sh` step scripts, embedded Python 3.11 (`huggingface_hub`, `pyarrow`), POSIX-shell test harness (repo convention — `tests/*.sh`), PyYAML for test-side Task parsing.

**Spec:** [inference-sim/tektonc-data-collection#67](https://github.com/inference-sim/tektonc-data-collection/issues/67), as scoped by its follow-up comment and the scope resolution recorded below.

## Global Constraints

- **Single-repo change.** Only `tektonc-data-collection` is touched. The Tekton Pipeline↔Task param contract MUST stay valid at every commit: `pipeline/pipeline.yaml` in `sim2real` declares and passes `traceShards` (`:69`, `:132-133`), so the `traceShards` param MUST continue to exist on this Task. Tekton rejects a Pipeline that passes a param the Task does not declare.
- **`traceShards` stays, honored-but-deprecated.** New semantics: a cap over the *discovered* file list — take the first N of the sorted list; empty means take all. Its `description` gains a deprecation note pointing at the descriptor follow-up.
- **Do NOT add a `traceInclude` param.** The whole scalar param surface is slated to collapse into a single `corpusSpec` document; adding a scalar now means deleting it shortly. `--include`-style selection belongs to that descriptor change.
- **`traceRevision` is added but DORMANT.** `sim2real`'s `pipeline/lib/tekton.py` does not emit it, so it is always `""` (= repo default branch) until a paired sim2real edit lands. The PR body must say so explicitly so revision pinning is not read as delivered.
- **No backward compatibility is owed for corpora, results, or the `trace:` descriptor** (no trace run has ever executed with sim2real, so nothing persisted depends on current behavior). Compatibility IS owed for the Pipeline↔Task param contract, per the first constraint.
- **Python version floor:** `python:3.11-slim` for both Python-bearing steps.
- **Pin the new dependency:** `huggingface_hub>=0.34`. The real requirement is `local_dir=` writing real files rather than cache symlinks (0.23+); 0.34 is just a recent, well-tested floor. The step never shells out to the `hf` CLI — it uses the Python API (`HfApi.dataset_info` / `list_repo_files` / `hf_hub_download`) throughout. Install via `pip install --quiet 'huggingface_hub>=0.34'`.
- **Tests must run with no cluster and no network.** Behavioral tests inject a fake `huggingface_hub` module via `PYTHONPATH`. Tests requiring `pyarrow` print `SKIP` and do not fail when it is absent.

---

## Verified Facts (do not re-derive; these were confirmed against the live HF API)

| Fact | Value |
|---|---|
| `Exgentic/agent-llm-traces` (v1) layout | 39 files, `data/train-00000-of-00039.parquet` |
| `Exgentic/agent-llm-traces-v2` layout | 9 files, `data/train/0000.parquet` |
| v1 decoded size | 2.77 GB total ⇒ ~71 MB/shard |
| v2 decoded size | 14.72 GB total ⇒ ~1.636 GB/shard |
| v2 top-level columns | 20 (⇒ 18 unused once `session_id` + `spans` are projected) |
| v2 span attributes | contains every field `build-otel` reads |
| `session_id` shape, BOTH datasets | `<12hex>_<8hex>` ⇒ `sid.split("_")[0]` dedup keeps working |

Current defect line numbers in `tekton/tasks/prepare-trace.yaml` (all verified at `36e98be`): `:110` step image, `:118` `: "${SHARDS:=39}"`, `:119` fixed `CORPUS=`, `:122` count-based guard, `:129` synthesized `FN=`, `:134` hardcoded `/resolve/main/` URL, `:164` fixed `CORPUS =` in Python, `:194` non-recursive glob, `:202` `read_table(...).to_pylist()`.

## The bug this plan fixes that the issue did not name

`CORPUS=/workspace/data/corpus` is a fixed path keyed by neither repo nor revision, and the download guard is `have >= SHARDS` (a bare *count*). On a shared PVC, v1's 39 files land in `corpus/`; a later v2 descriptor (9 shards) sees `39 >= 9`, short-circuits the download, and `build-otel` converts **v1 data** under a v2 `tracePath`. The `tracePath` guard cannot catch this — that path is a genuine cache miss, so the run proceeds on the wrong corpus. The same shared directory makes revision pinning inert: pinning a revision changes nothing when the directory is already populated. Fixing the fetch without fixing this ships a headline feature that is a no-op, so it is in scope here.

## File Structure

- **Modify** `tekton/tasks/prepare-trace.yaml` — the only production file. Three regions: params block (add `traceRevision`, reword `traceShards`), `download-corpus` step (full rewrite), `build-otel` step (corpus-dir handoff, recursive glob, streaming read, RSS report). Plus the Task-level `description`.
- **Create** `tests/test_prepare_trace_fetch.sh` — structural + behavioral tests for the fetch step and the corpus-dir contract.
- **Create** `tests/test_prepare_trace_build_otel.sh` — behavioral tests for the recursive glob and streaming read (pyarrow-gated).
- **Create** `tests/lib/extract_step.py` — shared helper: given a Task YAML path and a step name, print that step's `script`. Used by both test files so the extraction logic cannot drift between them.

---

## Task 1: Corpus-dir contract + layout-agnostic discovery in `download-corpus`

**Files:**
- Modify: `tekton/tasks/prepare-trace.yaml` — params block (~`:41-45`), `download-corpus` step (`:105-140`)
- Create: `tests/lib/extract_step.py`
- Create: `tests/test_prepare_trace_fetch.sh`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: **the `/workspace/corpus_dir` contract** — `download-corpus` writes the absolute resolved corpus directory (no trailing newline significance; readers must `.strip()`) to `/workspace/corpus_dir`. Task 2's `build-otel` reads it. Also produces the param `traceRevision` (type `string`, default `""`).
- Produces (test helper): `tests/lib/extract_step.py <task-yaml> <step-name>` → prints the step's `script` to stdout, exit 1 with a message on unknown step.

- [ ] **Step 1: Write the failing test — helper + structural assertions**

Create `tests/lib/extract_step.py`:

```python
#!/usr/bin/env python3
"""Print one step's `script` from a Tekton Task YAML.

Shared by the prepare-trace tests so the extraction logic cannot drift
between them. Usage: extract_step.py <task.yaml> <step-name>
"""
import sys

import yaml


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: extract_step.py <task.yaml> <step-name>", file=sys.stderr)
        return 2
    path, want = sys.argv[1], sys.argv[2]
    with open(path) as fh:
        task = yaml.safe_load(fh)
    for step in task["spec"]["steps"]:
        if step["name"] == want:
            sys.stdout.write(step["script"])
            return 0
    names = ", ".join(s["name"] for s in task["spec"]["steps"])
    print(f"no step named {want!r}; have: {names}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
```

Create `tests/test_prepare_trace_fetch.sh`:

```sh
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
echo "${STEP}" | grep -q 'of-\${TOTAL}' \
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

echo "${STEP}" | grep -q 'huggingface_hub>=0.34' \
  && pass "huggingface_hub is pinned (local_dir real-file semantics)" \
  || fail "huggingface_hub is not pinned to >=0.34"

echo "${STEP}" | grep -q 'params.traceRevision' \
  && pass "download-corpus reads the traceRevision param" \
  || fail "download-corpus does not read traceRevision"

# The corpus-dir contract Task 2 depends on.
echo "${STEP}" | grep -q '/workspace/corpus_dir' \
  && pass "download-corpus publishes the resolved corpus dir" \
  || fail "download-corpus does not write /workspace/corpus_dir"

# Param declared, defaulting to "" (= repo default branch).
python3 - "${TASK}" <<'PY' && pass "traceRevision param declared with default \"\"" \
                           || fail "traceRevision param missing or wrong default"
import sys, yaml
task = yaml.safe_load(open(sys.argv[1]))
p = {x["name"]: x for x in task["spec"]["params"]}
r = p.get("traceRevision")
sys.exit(0 if r and r.get("type") == "string" and r.get("default") == "" else 1)
PY

# traceShards MUST survive: sim2real's pipeline.yaml passes it, and Tekton
# rejects a Pipeline passing a param the Task does not declare.
python3 - "${TASK}" <<'PY' && pass "traceShards param still declared (Pipeline contract)" \
                           || fail "traceShards was removed — breaks sim2real pipeline.yaml"
import sys, yaml
task = yaml.safe_load(open(sys.argv[1]))
sys.exit(0 if any(p["name"] == "traceShards" for p in task["spec"]["params"]) else 1)
PY

echo
if ${PASS}; then echo "ALL PASS"; exit 0; else echo "FAILURES"; exit 1; fi
```

- [ ] **Step 2: Run it to verify it fails**

Run: `sh tests/test_prepare_trace_fetch.sh`
Expected: FAIL lines for the synthesized filenames, the `/resolve/main/` URL, the image, the `huggingface_hub` pin, `traceRevision` (both the param and the step read), and `/workspace/corpus_dir`. The `traceShards` assertion should already PASS.

- [ ] **Step 3: Add the `traceRevision` param and reword `traceShards`**

In `tekton/tasks/prepare-trace.yaml`, replace the `traceShards` param block with:

```yaml
    - name: traceShards
      type: string
      default: ""
      description: >-
        DEPRECATED cap on the number of parquet files to fetch: after the
        dataset's files are DISCOVERED, the sorted list is truncated to the
        first N. Empty (the default) fetches all discovered files. Retained
        because sim2real's pipeline.yaml passes it; a shard *prefix* is a
        benchmark-biased subsample, not a random one (for
        Exgentic/agent-llm-traces: shards 0-1 appworld, 13 swebench, 20
        swebench+tau2_airline, 26 tau2_retail, 38 tau2_telecom), so prefer
        deliberate selection once the descriptor grows include patterns.
    - name: traceRevision
      type: string
      default: ""
      description: >-
        Optional git revision (branch, tag, or commit SHA) of the HF dataset to
        fetch. Empty (the default) means the repo's default branch. The step
        RESOLVES this to a commit SHA and keys the on-PVC corpus directory by
        it, so a moving ref that advances upstream lands in a fresh directory
        instead of mixing with the previously downloaded files.
        NOTE: sim2real does not emit this param yet, so today it is always ""
        — the plumbing is here, but pinning is not reachable from a descriptor
        until the paired sim2real change lands.
```

- [ ] **Step 4: Rewrite the `download-corpus` step**

Replace the entire `download-corpus` step (from `- name: download-corpus` through the end of its script, i.e. `:105-140`) with:

```yaml
    # ---- step 1: discover + download the corpus parquet files if absent ----
    # Layout-agnostic (#67): the file list is DISCOVERED from the HF repo rather
    # than synthesized from a shard-count format, so both of HF's standard
    # layouts work — sharded-split (data/train-00000-of-00039.parquet) and
    # split-as-directory (data/train/0000.parquet).
    #
    # The corpus directory is keyed by <repo>@<resolved-sha>, which is what makes
    # revision pinning meaningful and what stops two datasets from colliding:
    # the previous fixed /workspace/data/corpus plus a count-based guard let a
    # 9-file v2 descriptor see v1's 39 files, skip the download, and silently
    # convert v1 data. Idempotency is now per FILE against the discovered list,
    # which is sound however the shard counts differ.
    - name: download-corpus
      image: python:3.11-slim
      script: |
        #!/bin/sh
        set -e
        [ -f /workspace/skip ] && { echo "skip"; exit 0; }
        pip install --quiet 'huggingface_hub>=0.34'
        export REPO="$(params.traceSource)"
        export REV="$(params.traceRevision)"
        export SHARDS="$(params.traceShards)"
        python3 - <<'PYEOF'
        import os
        import re
        import sys

        from huggingface_hub import HfApi, hf_hub_download

        repo = (os.environ.get("REPO") or "").strip()
        repo = repo[3:] if repo.startswith("hf:") else repo   # strip hf: prefix
        if not repo:
            sys.exit("download-corpus: traceSource is empty — nothing to fetch")
        rev = (os.environ.get("REV") or "").strip() or None
        shards_raw = (os.environ.get("SHARDS") or "").strip()
        shards = int(shards_raw) if shards_raw else 0        # 0 / empty => all

        api = HfApi()
        # Resolve the ref to a commit SHA so a moving ref (e.g. "main") that
        # advances upstream lands in a NEW corpus dir instead of mixing files.
        info = api.dataset_info(repo, revision=rev)
        sha = info.sha
        if not sha:
            sys.exit("download-corpus: could not resolve a commit sha for "
                     "%s@%s" % (repo, rev or "<default>"))

        # Discover, don't synthesize. Sorted so the deprecated shard cap is
        # deterministic.
        files = sorted(
            f for f in api.list_repo_files(repo, repo_type="dataset", revision=sha)
            if f.endswith(".parquet")
        )
        if not files:
            sys.exit("download-corpus: no .parquet files found in %s@%s"
                     % (repo, sha))
        discovered = len(files)
        if shards > 0:
            files = files[:shards]

        # <repo>@<sha>, with the repo's "/" and anything else awkward flattened.
        slug = re.sub(r"[^A-Za-z0-9._-]", "_", repo)
        corpus = "/workspace/data/corpus/%s@%s" % (slug, sha)
        os.makedirs(corpus, exist_ok=True)

        # Per-file idempotency against the discovered list. The old guard
        # compared a bare COUNT against the requested shard count, which is
        # unsound in both directions once shard counts vary between datasets.
        missing = [f for f in files
                   if not os.path.exists(os.path.join(corpus, f))]
        print("download-corpus: %s@%s discovered=%d selected=%d present=%d "
              "missing=%d shard_cap=%s"
              % (repo, sha, discovered, len(files), len(files) - len(missing),
                 len(missing), shards or "none"))
        for f in missing:
            print("downloading %s" % f)
            # local_dir= writes REAL files (not cache symlinks) and PRESERVES
            # repo structure, so a split-as-directory dataset lands at
            # <corpus>/data/train/0000.parquet — build-otel globs recursively.
            hf_hub_download(repo, filename=f, repo_type="dataset",
                            revision=sha, local_dir=corpus)

        # Publish the resolved dir for build-otel (same cross-step marker
        # mechanism as /workspace/skip). build-otel must not rebuild this path.
        with open("/workspace/corpus_dir", "w") as fh:
            fh.write(corpus)
        print("download-corpus: %d parquet file(s) ready in %s"
              % (len(files), corpus))
        PYEOF
```

- [ ] **Step 5: Run the structural test to verify it passes**

Run: `sh tests/test_prepare_trace_fetch.sh`
Expected: ALL PASS.

- [ ] **Step 6: Add the behavioral test (fake `huggingface_hub`, no network)**

Append to `tests/test_prepare_trace_fetch.sh`, immediately before the final `echo` / verdict block:

```sh
# ────────────────────────────────────────────────────────────
# Part 2 — behavioral: run the embedded Python against a FAKE
# huggingface_hub. Pins the two properties the issue is about, plus the
# cross-dataset collision that motivated keying the corpus dir.
# ────────────────────────────────────────────────────────────
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

# Pull just the heredoc'd Python out of the step script.
python3 - "${TASK}" > "${TMP}/download.py" <<'PY'
import sys, yaml
task = yaml.safe_load(open(sys.argv[1]))
step = next(s for s in task["spec"]["steps"] if s["name"] == "download-corpus")
lines = step["script"].splitlines()
start = next(i for i, l in enumerate(lines) if l.strip().endswith("<<'PYEOF'")) + 1
end = next(i for i, l in enumerate(lines) if l.strip() == "PYEOF")
body = lines[start:end]
indent = min((len(l) - len(l.lstrip()) for l in body if l.strip()), default=0)
print("\n".join(l[indent:] for l in body))
PY

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
DOWNLOADS = []


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
    DOWNLOADS.append(dest)
    with open(os.path.join(os.environ["DL_LOG"]), "a") as fh:
        fh.write(dest + "\n")
    return dest
PY

# run <repo> <rev> <shards> -> prints the resolved corpus dir; appends the
# files it downloaded to ${TMP}/dl.log (truncated per run).
run_dl() {
  : > "${TMP}/dl.log"
  DL_LOG="${TMP}/dl.log" \
  PYTHONPATH="${TMP}/fake" \
  REPO="$1" REV="$2" SHARDS="$3" \
  python3 "${TMP}/download.py" > "${TMP}/out.txt" 2>&1
  rc=$?
  [ ${rc} -eq 0 ] || { echo "--- step output ---"; cat "${TMP}/out.txt"; }
  return ${rc}
}

# Redirect the step's absolute /workspace paths into the sandbox.
sed -i.bak "s#/workspace#${TMP}/workspace#g" "${TMP}/download.py"
mkdir -p "${TMP}/workspace/data"

# (a) split-as-directory (v2) — the layout that used to 404 outright.
if run_dl "hf:Exgentic/agent-llm-traces-v2" "" ""; then
  n=$(wc -l < "${TMP}/dl.log" | tr -d ' ')
  [ "${n}" = "9" ] \
    && pass "v2 split-as-directory: discovered and fetched all 9 files" \
    || fail "v2: expected 9 downloads, got ${n}"
  grep -q 'data/train/0000.parquet' "${TMP}/dl.log" \
    && pass "v2: nested path preserved under the corpus dir" \
    || fail "v2: nested data/train/ path not preserved"
else
  fail "v2 fetch raised"
fi
V2_DIR="$(cat "${TMP}/workspace/corpus_dir")"

# (b) sharded-split (v1) still works, and lands in a DIFFERENT directory.
if run_dl "hf:Exgentic/agent-llm-traces" "" ""; then
  n=$(wc -l < "${TMP}/dl.log" | tr -d ' ')
  [ "${n}" = "39" ] \
    && pass "v1 sharded-split: discovered and fetched all 39 files" \
    || fail "v1: expected 39 downloads, got ${n}"
else
  fail "v1 fetch raised"
fi
V1_DIR="$(cat "${TMP}/workspace/corpus_dir")"

[ "${V1_DIR}" != "${V2_DIR}" ] \
  && pass "the two datasets resolve to different corpus dirs" \
  || fail "COLLISION: both datasets share corpus dir ${V1_DIR}"

# (c) THE BUG: with a shared dir, v1's 39 files made a 9-shard v2 request
# skip its download (39 >= 9) and convert v1 data. Re-running v2 now must
# still fetch into v2's own dir, never see v1's files.
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

# (e) the deprecated shard cap still truncates the DISCOVERED list.
if run_dl "hf:Exgentic/agent-llm-traces" "" "3"; then
  n=$(wc -l < "${TMP}/dl.log" | tr -d ' ')
  [ "${n}" = "3" ] \
    && pass "traceShards=3 caps the discovered list to 3 files" \
    || fail "traceShards=3 fetched ${n} file(s)"
else
  fail "shard-capped fetch raised"
fi
```

- [ ] **Step 7: Run the full test file**

Run: `sh tests/test_prepare_trace_fetch.sh`
Expected: ALL PASS. If (c) or (d) fails, the corpus dir is not actually keyed — re-check the `slug`/`sha` interpolation.

- [ ] **Step 8: Commit**

```bash
git add tekton/tasks/prepare-trace.yaml tests/lib/extract_step.py tests/test_prepare_trace_fetch.sh
git commit -m "fix(prepare-trace): discover HF corpus files instead of synthesizing shard names (#67)

Replace the curl loop over synthesized train-NNNNN-of-NNNNN.parquet names
with huggingface_hub discovery, so both of HF's standard dataset layouts
work. Key the on-PVC corpus dir by <repo>@<resolved-sha> and make the
download guard per-file against the discovered list: the previous fixed
/workspace/data/corpus plus a count-based guard let a 9-file v2 descriptor
see v1's 39 files, skip the download, and silently convert v1 data.

Add a traceRevision param (dormant until sim2real emits it). traceShards is
retained honored-but-deprecated as a cap over the discovered list, because
sim2real's pipeline.yaml passes it and Tekton rejects a Pipeline passing a
param the Task does not declare."
```

---

## Task 2: Recursive glob + streaming parquet read in `build-otel`

**Files:**
- Modify: `tekton/tasks/prepare-trace.yaml` — `build-otel` step (`:142-260`), Task-level `description` (`:7-20`)
- Create: `tests/test_prepare_trace_build_otel.sh`

**Interfaces:**
- Consumes: `/workspace/corpus_dir` (Task 1) — the resolved absolute corpus directory; read it and `.strip()`.
- Produces: nothing later tasks consume. `build-otel` keeps writing `/workspace/data/otel/corpus.jsonl` in exactly the same JSONL shape (`{"spans": [...]}` per line), so `blis convert otel` in step 3 is untouched.

- [ ] **Step 1: Write the failing test**

Create `tests/test_prepare_trace_build_otel.sh`:

```sh
#!/bin/sh
# Tests the corpus discovery + streaming read in prepare-trace's build-otel
# step (inference-sim/tektonc-data-collection#67):
#   - Part 1: structural — reads the corpus dir from the marker rather than
#     hardcoding it, globs RECURSIVELY (hf preserves repo structure), and
#     streams with column projection instead of read_table().to_pylist()
#   - Part 2: behavioral — the same sessions come out for BOTH layouts (flat
#     v1-style filenames and nested data/train/), which is what proves the
#     recursive glob and the batched read did not change the corpus
#
# Part 2 needs pyarrow and SKIPs without it.
# Run with: sh tektonc-data-collection/tests/test_prepare_trace_build_otel.sh

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

STEP="$(python3 "${EXTRACT}" "${TASK}" build-otel)" || {
  echo "FAIL: could not extract the build-otel step"; exit 1; }

# ────────────────────────────────────────────────────────────
# Part 1 — structural
# ────────────────────────────────────────────────────────────
echo "${STEP}" | grep -q 'read_table' \
  && fail "build-otel still calls read_table() (materializes a whole shard)" \
  || pass "whole-shard read_table() is gone"

echo "${STEP}" | grep -q 'iter_batches' \
  && pass "build-otel streams with iter_batches" \
  || fail "build-otel does not use iter_batches"

echo "${STEP}" | grep -q 'columns=' \
  && pass "build-otel projects columns (drops v2's 18 unused ones)" \
  || fail "build-otel does not project columns"

echo "${STEP}" | grep -q 'corpus_dir' \
  && pass "build-otel reads the corpus dir from the marker" \
  || fail "build-otel does not read /workspace/corpus_dir"

echo "${STEP}" | grep -q 'recursive=True' \
  && pass "build-otel globs recursively (hf preserves repo structure)" \
  || fail "build-otel glob is not recursive — nested shards would be invisible"

echo "${STEP}" | grep -q 'ru_maxrss' \
  && pass "build-otel reports peak RSS (makes the memory criterion measurable)" \
  || fail "build-otel does not report peak RSS"

# ────────────────────────────────────────────────────────────
# Part 2 — behavioral, both layouts
# ────────────────────────────────────────────────────────────
python3 -c 'import pyarrow' 2>/dev/null || {
  echo "SKIP: pyarrow not available — skipping the behavioral half"
  echo
  if ${PASS}; then echo "ALL PASS (structural only)"; exit 0; else echo "FAILURES"; exit 1; fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

python3 - "${TASK}" > "${TMP}/build.py" <<'PY'
import sys, yaml
task = yaml.safe_load(open(sys.argv[1]))
step = next(s for s in task["spec"]["steps"] if s["name"] == "build-otel")
lines = step["script"].splitlines()
start = next(i for i, l in enumerate(lines) if l.strip().endswith("<<'PYEOF'")) + 1
end = next(i for i, l in enumerate(lines) if l.strip() == "PYEOF")
body = lines[start:end]
indent = min((len(l) - len(l.lstrip()) for l in body if l.strip()), default=0)
print("\n".join(l[indent:] for l in body))
PY

sed -i.bak "s#/workspace#${TMP}/workspace#g" "${TMP}/build.py"

# Write the same 6 sessions into a given layout, then run build-otel over it.
# Schema mirrors the real datasets: spans is a list<struct> whose attributes
# struct carries the gen_ai.* fields build-otel reads.
build_corpus() {  # $1 = layout: flat|nested
  python3 - "$1" "${TMP}" <<'PY'
import os, sys
import pyarrow as pa
import pyarrow.parquet as pq

layout, tmp = sys.argv[1], sys.argv[2]
corpus = os.path.join(tmp, "workspace", "data", "corpus", "ds@sha")
rel = "data/train-00000-of-00001.parquet" if layout == "flat" else "data/train/0000.parquet"
dest = os.path.join(corpus, rel)
os.makedirs(os.path.dirname(dest), exist_ok=True)

attrs = pa.struct([
    ("gen_ai.request.model", pa.string()),
    ("gen_ai.usage.input_tokens", pa.int64()),
    ("gen_ai.usage.output_tokens", pa.int64()),
])
span = pa.struct([
    ("span_id", pa.string()), ("name", pa.string()),
    ("start_time", pa.string()),
    ("status", pa.struct([("code", pa.int64())])),
    ("attributes", attrs),
])
schema = pa.schema([
    ("session_id", pa.string()), ("spans", pa.list_(span)),
    ("unused_blob", pa.string()),   # stands in for v2's 18 unused columns
])

def mk(sid, n, tokens=(10, 5)):
    return {
        "session_id": sid,
        "spans": [{
            "span_id": "s%d" % i, "name": "chat",
            "start_time": "2026-01-01T00:00:%02dZ" % i,
            "status": {"code": 1},
            "attributes": {
                "gen_ai.request.model": "m",
                "gen_ai.usage.input_tokens": tokens[0],
                "gen_ai.usage.output_tokens": tokens[1],
            },
        } for i in range(n)],
        "unused_blob": "x" * 64,
    }

rows = [
    mk("aaaaaaaaaaaa_00000001", 3),   # kept
    mk("aaaaaaaaaaaa_00000002", 3),   # same conversation -> dedup drops one
    mk("bbbbbbbbbbbb_00000003", 3),   # kept
    mk("cccccccccccc_00000004", 1),   # dropped: < min_rounds
    mk("dddddddddddd_00000005", 3, (0, 5)),   # dropped: unusable input tokens
    mk("eeeeeeeeeeee_00000006", 3),   # kept
]
pq.write_table(pa.Table.from_pylist(rows, schema=schema), dest, row_group_size=2)
print(corpus)
PY
}

run_build() {  # $1 = layout
  rm -rf "${TMP}/workspace"
  mkdir -p "${TMP}/workspace/data"
  CORPUS="$(build_corpus "$1")" || return 1
  printf '%s' "${CORPUS}" > "${TMP}/workspace/corpus_dir"
  MIN_ROUNDS=2 SPLIT=all DEDUP_BY_CONVERSATION=1 SHUFFLE_SEED=42 \
    python3 "${TMP}/build.py" > "${TMP}/out_$1.txt" 2>&1
  rc=$?
  [ ${rc} -eq 0 ] || { echo "--- build-otel output ($1) ---"; cat "${TMP}/out_$1.txt"; }
  return ${rc}
}

for layout in flat nested; do
  if run_build "${layout}"; then
    JSONL="${TMP}/workspace/data/otel/corpus.jsonl"
    n=$(wc -l < "${JSONL}" | tr -d ' ')
    # 6 sessions -> 4 usable (>=2 usable spans) -> dedup by conversation
    # collapses the two aaaa... sessions -> 3 written.
    [ "${n}" = "3" ] \
      && pass "${layout} layout: 3 sessions written (filter + dedup as before)" \
      || fail "${layout} layout: expected 3 sessions, got ${n}"
    cp "${JSONL}" "${TMP}/jsonl_${layout}"
  else
    fail "${layout} layout: build-otel raised"
  fi
done

if [ -f "${TMP}/jsonl_flat" ] && [ -f "${TMP}/jsonl_nested" ]; then
  cmp -s "${TMP}/jsonl_flat" "${TMP}/jsonl_nested" \
    && pass "both layouts produce a byte-identical corpus" \
    || fail "layouts diverge — discovery is leaking file paths into the output"
fi

grep -q 'peak_rss_mb=' "${TMP}/out_nested.txt" \
  && pass "build-otel logs peak RSS for the run" \
  || fail "build-otel did not log peak RSS"

echo
if ${PASS}; then echo "ALL PASS"; exit 0; else echo "FAILURES"; exit 1; fi
```

- [ ] **Step 2: Run it to verify it fails**

Run: `sh tests/test_prepare_trace_build_otel.sh`
Expected: FAIL on `read_table` still present, `iter_batches` absent, `columns=` absent, `corpus_dir` absent, `recursive=True` absent, `ru_maxrss` absent. Part 2 will also fail (the step still hardcodes the corpus path).

- [ ] **Step 3: Rewrite the `build-otel` corpus discovery and read loop**

In the `build-otel` step's embedded Python, replace the constants block:

```python
        CORPUS  = "/workspace/data/corpus"
        OUT_DIR = "/workspace/data/otel"
```

with:

```python
        # The corpus dir is resolved by download-corpus (keyed <repo>@<sha>) and
        # handed over via this marker — do NOT rebuild the path here, or the two
        # steps can disagree about which dataset/revision is being converted.
        try:
            with open("/workspace/corpus_dir") as _fh:
                CORPUS = _fh.read().strip()
        except OSError as exc:
            sys.exit("build-otel: cannot read /workspace/corpus_dir (%s) — "
                     "download-corpus must run first" % exc)
        if not CORPUS:
            sys.exit("build-otel: /workspace/corpus_dir is empty")
        OUT_DIR = "/workspace/data/otel"
```

Replace the non-recursive glob:

```python
        files = sorted(glob.glob(os.path.join(CORPUS, "*.parquet")))
```

with:

```python
        # RECURSIVE: hf_hub_download preserves repo structure, so a
        # split-as-directory dataset lands at <CORPUS>/data/train/0000.parquet.
        # A non-recursive glob finds nothing there and the step would
        # "succeed" on an empty corpus.
        files = sorted(glob.glob(os.path.join(CORPUS, "**", "*.parquet"),
                                recursive=True))
        if not files:
            sys.exit("build-otel: no .parquet files under %s" % CORPUS)
```

Replace the whole-shard read:

```python
        for f in files:
            for r in pq.read_table(f).to_pylist():
```

with:

```python
        for f in files:
            # Stream in batches with column projection. read_table().to_pylist()
            # materialized an entire shard as Python objects — fine at v1's
            # ~71 MB/shard, fatal at v2's ~1.6 GB/shard. The projection also
            # drops v2's 18 unused top-level columns (notably the large
            # gen_ai.{input,output}.messages payloads).
            pf = pq.ParquetFile(f)
            for batch in pf.iter_batches(batch_size=BATCH_SIZE,
                                         columns=["session_id", "spans"]):
              for r in batch.to_pylist():
```

> **Indentation note:** the original loop body is indented under `for r in ...`. Adding the `for batch in ...` level means the whole body must shift one level deeper. Do this by re-indenting the existing body block (from `scanned += 1` through `records.append((sid, rec_spans))`) by two spaces — do not rewrite its logic. The `for r in batch.to_pylist():` line above is deliberately shown at a 14-space offset so the existing body's 16-space offset still nests correctly under it.

Add near the top of the Python program, after the imports:

```python
        # Batch size for the streaming read. Small because a single v2 row can
        # decode to ~1.5 MB; 32 keeps a batch well under 50 MB.
        BATCH_SIZE = int(os.environ.get("BATCH_SIZE") or 32)
```

and add `import resource` to the import line so peak RSS can be reported.

Finally, extend the closing summary print so the memory criterion is
measurable rather than merely asserted. After the existing
`print("build-otel: wrote %s" % OUT)` line, add:

```python
        # ru_maxrss is bytes on macOS, kibibytes on Linux (the step runs on
        # Linux). Reported so #67's memory criterion can be checked from the
        # step log; no resources.limits is imposed here, since a wrong limit
        # would OOMKill legitimate runs rather than reveal anything.
        print("build-otel: peak_rss_mb=%.1f files=%d batch_size=%d"
              % (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0,
                 len(files), BATCH_SIZE))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `sh tests/test_prepare_trace_build_otel.sh`
Expected: ALL PASS (create a `.venv` with pyarrow first so Part 2 actually executes rather than SKIPs — see Task 3 Step 1).

- [ ] **Step 5: Update the Task-level `description`**

The Task `description` still says the shards are downloaded from a
`resolve/main` URL under `data/train-<NNNNN>-of-<TOTAL>.parquet`. Replace that
clause so it describes discovery, and note the corpus keying:

```yaml
  description: >-
    Prepare a recorded TraceV2 corpus on the data PVC for trace-input observe
    (inference-sim/tektonc-data-collection#62). Short-circuits when traceSpec is
    empty (generative workload) or when the target TraceV2 is already cached on
    the PVC. Otherwise runs the corpus->TraceV2 chain: DISCOVER and download the
    corpus parquet files from the HF dataset via huggingface_hub (layout-agnostic
    — both sharded-split and split-as-directory work; #67), into a corpus dir
    keyed by <repo>@<resolved-sha> so two datasets or revisions never share a
    cache -> build OTel JSONL (inline port of build_otel_corpus.py: usable-span +
    min_rounds filter, deterministic SHA1 train/test split; no branching filter,
    see #63) -> blis convert otel. Every field the steps need arrives as a
    SEPARATE SCALAR param (traceSource, traceRevision, traceShards,
    traceMinRounds, traceSplit, traceContextGrowth) so no in-container YAML
    parsing of the compact traceSpec is required. One task, guarded steps; every
    step no-ops when the guard marker is present, so a generative run's pod
    starts and exits in seconds.
```

- [ ] **Step 6: Commit**

```bash
git add tekton/tasks/prepare-trace.yaml tests/test_prepare_trace_build_otel.sh
git commit -m "fix(prepare-trace): recursive corpus glob + streaming parquet read (#67)

build-otel now takes the corpus dir from the /workspace/corpus_dir marker
download-corpus publishes, globs it recursively (hf_hub_download preserves
repo structure, so a split-as-directory dataset lands at
<corpus>/data/train/0000.parquet and a non-recursive glob would silently
find nothing), and streams each shard via ParquetFile.iter_batches with
column projection instead of read_table().to_pylist() — which materialized
an entire shard as Python objects, fine at v1's ~71 MB/shard and fatal at
v2's ~1.6 GB/shard. Peak RSS is logged so the memory criterion is
measurable from the step log."
```

---

## Task 3: Verify end-to-end, sweep for stale references, open the PR

**Files:**
- Modify: none expected (this task is verification + PR authoring; fix anything the sweep turns up)

**Interfaces:**
- Consumes: the finished Task 1 + Task 2 changes.
- Produces: a pushed branch and an open PR.

- [ ] **Step 1: Create a venv and run BOTH test files for real**

```bash
python3 -m venv .venv
.venv/bin/pip install --quiet pyarrow PyYAML
PATH="$(pwd)/.venv/bin:${PATH}" sh tests/test_prepare_trace_fetch.sh
PATH="$(pwd)/.venv/bin:${PATH}" sh tests/test_prepare_trace_build_otel.sh
```

Expected: `ALL PASS` from both, with no `SKIP` line in the second (a SKIP means pyarrow did not get picked up and Part 2 never ran — fix the PATH rather than accepting it). `.venv/` is already gitignored.

- [ ] **Step 2: Confirm the whole task YAML still parses and every param is wired**

```bash
.venv/bin/python - <<'PY'
import yaml
t = yaml.safe_load(open("tekton/tasks/prepare-trace.yaml"))
names = [p["name"] for p in t["spec"]["params"]]
print("params:", names)
assert "traceShards" in names, "Pipeline contract broken"
assert "traceRevision" in names
print("steps:", [s["name"] for s in t["spec"]["steps"]])
PY
```

Expected: `traceShards` and `traceRevision` both present, four steps unchanged in name and order (`guard`, `download-corpus`, `build-otel`, `convert`).

- [ ] **Step 3: Confirm the parent repo was not touched**

```bash
git status --short
git -C /Users/kalantar/projects/go.workspace/src/github.com/inference-sim/sim2real status --short
```

Expected: the worktree shows only the intended files; the parent `sim2real` repo shows no NEW modifications from this session (a pre-existing dirty `.gitmodules` / `pipeline/lib/tekton.py` from before this session is not ours — do not revert or commit those).

- [ ] **Step 4: Sweep for stale references**

Changed basenames/symbols to grep for: `prepare-trace`, `traceShards`, `traceRevision`, `data/corpus`, `corpus_dir`, `train-000`, `read_table`, `build_otel`.

```bash
grep -rn 'prepare-trace\|traceShards\|data/corpus\|train-000\|read_table' \
  --exclude-dir=.git --exclude-dir=.venv . | grep -v '^./tekton/tasks/prepare-trace.yaml' \
  | grep -v '^./tests/'
```

Known state at plan time: the only other hit anywhere in the repo is
`tekton/tasks/run-workload-blis-observe-binary.yaml`, which consumes
`tracePath` (the TraceV2 output) and never the corpus dir — so it needs no
change. There are no markdown references to `prepare-trace` at all, and this
repo has no `.github/workflows/`. Record in the PR body what was swept and
that nothing outside the task needed updating; if the grep now shows a new
hit, fix it in this task.

- [ ] **Step 5: Push and open the PR**

```bash
git push -u origin issue-67-layout-agnostic-fetch
unset GITHUB_TOKEN GH_TOKEN; gh pr create --title "fix(prepare-trace): layout-agnostic HF corpus fetch, revision-keyed cache, streaming parquet read (#67)" --body-file /tmp/pr-67-body.md
```

The PR body MUST state:
- `Closes #67`
- Base: `main`
- That `traceRevision` is **dormant** until `sim2real`'s `pipeline/lib/tekton.py` emits it, so revision pinning is plumbed but not yet reachable from a descriptor.
- That `traceShards` was deliberately KEPT (Pipeline↔Task param contract) and `traceInclude` deliberately NOT added (the param surface is slated to collapse into `corpusSpec`).
- The corpus-keying bug found during vetting that the issue did not name, and why it belongs in this PR (revision pinning is inert without it).
- That AC-4 was made measurable via a logged `peak_rss_mb` rather than a hard `resources.limits`, and why.
- What the stale-reference sweep covered.
- That AC "v2 completes the chain end-to-end" is NOT verified by these tests — they cover discovery, keying, and the read; a real cluster run is still required.

---

## Self-Review

**1. Spec coverage.**

| Issue requirement | Task |
|---|---|
| Fix 1 — discover files instead of synthesizing names | Task 1 Step 4 |
| Fix 1 caveat — recursive glob after `hf` preserves structure | Task 2 Step 3 |
| Fix 2 — optional `traceRevision`, default `""` | Task 1 Step 3 |
| Fix 3 (as amended) — `traceShards` as a cap over discovered files; unsound guard replaced | Task 1 Steps 3-4 |
| Fix 4 — streaming batched read + column projection | Task 2 Step 3 |
| AC — v2 completes the chain | Partially: Task 2 Part 2 proves both layouts convert identically; a real cluster run is NOT covered and the PR must say so |
| AC — pinned `traceRevision` honoured | Task 1 Step 6 case (d) |
| AC — bounded memory | Task 2 Step 3 (`peak_rss_mb` log) + Step 1 structural assertion |
| AC — both guards still short-circuit | `guard` step untouched; the `[ -f /workspace/skip ]` early-exit is preserved verbatim in the rewritten step (Task 1 Step 4) |
| Out of scope — descriptor `revision` surfacing, `--include` | Explicitly excluded by Global Constraints |

**2. Placeholder scan.** No TBD/TODO/"handle edge cases"/"similar to Task N". Every code step carries literal code. The one prose-only instruction (Task 2 Step 3's re-indentation note) describes a mechanical transform on code shown in the same step.

**3. Type consistency.** `/workspace/corpus_dir` is written in Task 1 Step 4 and read in Task 2 Step 3 — same literal path, both sides `.strip()`-tolerant (writer emits no trailing newline, reader strips). `tests/lib/extract_step.py`'s CLI (`<task.yaml> <step-name>`) is used identically by both test files. `BATCH_SIZE` is defined and consumed within Task 2 Step 3. Param names `traceRevision` / `traceShards` are spelled consistently across params block, step scripts, and both test files.

**Gap accepted deliberately:** `records` still accumulates every selected session in memory. This is sound because the projection drops the large `gen_ai.{input,output}.messages` payloads and the split filter discards ~70% of sessions before append — the retained per-span dicts are small. Worth a line in the PR body, not a fix here.
