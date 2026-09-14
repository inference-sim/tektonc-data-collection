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

# Comment-stripped view of the step. The "is it gone?" assertions below must
# look at CODE only: the rewritten step legitimately names read_table() in a
# comment explaining what it replaced, and a test that forbids documenting
# that would be testing prose, not behavior. Strips whole-line comments and
# PEP8 inline (" # ") comments; the step has no "#" inside string literals.
STEP_CODE="$(printf '%s\n' "${STEP}" | sed -e 's/[[:space:]]#[[:space:]].*$//' \
                                           -e '/^[[:space:]]*#/d')"

# ────────────────────────────────────────────────────────────
# Part 1 — structural
# ────────────────────────────────────────────────────────────
echo "${STEP_CODE}" | grep -q 'read_table' \
  && fail "build-otel still calls read_table() (materializes a whole shard)" \
  || pass "whole-shard read_table() is gone from the code"

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

if ! python3 - "${TASK}" > "${TMP}/build.py" <<'PY'
import sys, yaml
task = yaml.safe_load(open(sys.argv[1]))
step = next(s for s in task["spec"]["steps"] if s["name"] == "build-otel")
lines = step["script"].splitlines()
try:
    start = next(i for i, l in enumerate(lines) if l.strip().endswith("<<'PYEOF'")) + 1
    end = next(i for i, l in enumerate(lines) if l.strip() == "PYEOF")
except StopIteration:
    sys.exit("build-otel has no <<'PYEOF' ... PYEOF python program")
body = lines[start:end]
indent = min((len(l) - len(l.lstrip()) for l in body if l.strip()), default=0)
print("\n".join(l[indent:] for l in body))
PY
then
  fail "could not extract an embedded python program from build-otel"
  echo; echo "FAILURES"; exit 1
fi
[ -s "${TMP}/build.py" ] || {
  fail "extracted build-otel program is empty"; echo; echo "FAILURES"; exit 1; }

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
rel = ("data/train-00000-of-00001.parquet" if layout == "flat"
       else "data/train/0000.parquet")
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
    mk("aaaaaaaaaaaa_00000001", 3),            # kept
    mk("aaaaaaaaaaaa_00000002", 3),            # same conversation -> dedup drops one
    mk("bbbbbbbbbbbb_00000003", 3),            # kept
    mk("cccccccccccc_00000004", 1),            # dropped: < min_rounds
    mk("dddddddddddd_00000005", 3, (0, 5)),    # dropped: unusable input tokens
    mk("eeeeeeeeeeee_00000006", 3),            # kept
]
# Several row groups so iter_batches actually iterates more than once.
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

if [ -f "${TMP}/out_nested.txt" ]; then
  grep -q 'peak_rss_mb=' "${TMP}/out_nested.txt" \
    && pass "build-otel logs peak RSS for the run" \
    || fail "build-otel did not log peak RSS"
fi

# An absent marker must fail loudly: build-otel converting a stale hardcoded
# path is exactly the silent-wrong-corpus failure #67 is about.
rm -rf "${TMP}/workspace"
mkdir -p "${TMP}/workspace/data"
if MIN_ROUNDS=2 SPLIT=all python3 "${TMP}/build.py" > "${TMP}/out_nomarker.txt" 2>&1; then
  fail "build-otel succeeded with no /workspace/corpus_dir marker"
else
  grep -q 'corpus_dir' "${TMP}/out_nomarker.txt" \
    && pass "missing corpus_dir marker fails with a clear message" \
    || { fail "missing marker failed without naming corpus_dir"; cat "${TMP}/out_nomarker.txt"; }
fi

# A marker pointing at a dir with no parquet must fail rather than write an
# empty corpus — the issue's explicit warning about the non-recursive glob.
EMPTY="${TMP}/workspace/data/corpus/empty@sha"
mkdir -p "${EMPTY}"
printf '%s' "${EMPTY}" > "${TMP}/workspace/corpus_dir"
if MIN_ROUNDS=2 SPLIT=all python3 "${TMP}/build.py" > "${TMP}/out_empty.txt" 2>&1; then
  fail "build-otel succeeded on a corpus dir with no parquet files"
else
  grep -q 'no .parquet files' "${TMP}/out_empty.txt" \
    && pass "an empty corpus dir fails instead of 'succeeding' silently" \
    || { fail "empty corpus failed without the expected message"; cat "${TMP}/out_empty.txt"; }
fi

echo
if ${PASS}; then echo "ALL PASS"; exit 0; else echo "FAILURES"; exit 1; fi
