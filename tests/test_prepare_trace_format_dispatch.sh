#!/bin/sh
# Tests the traceFormat dispatch in the prepare-trace task
# (inference-sim/tektonc-data-collection#68):
#   - Part 1: structural — the two new params exist with the defaults that keep
#     the otel path byte-identical, and the convert step no longer hardcodes its
#     input.
#   - Part 2: behavioral — the guard's format validation, the format-driven
#     discovery filter in download-corpus (against the same FAKE
#     huggingface_hub the fetch test uses), and the convert step's dispatch
#     against a FAKE blis that records its argv. The argv assertions are the
#     point of this file: they pin the exact command line each format produces,
#     including the flags that would otherwise silently fall back to a
#     converter default.
#
# Run with: sh tektonc-data-collection/tests/test_prepare_trace_format_dispatch.sh

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

GUARD="$(python3 "${EXTRACT}" "${TASK}" guard)"       || { echo "FAIL: no guard step"; exit 1; }
DL="$(python3 "${EXTRACT}" "${TASK}" download-corpus)" || { echo "FAIL: no download-corpus"; exit 1; }
BO="$(python3 "${EXTRACT}" "${TASK}" build-otel)"     || { echo "FAIL: no build-otel"; exit 1; }
CONV="$(python3 "${EXTRACT}" "${TASK}" convert)"      || { echo "FAIL: no convert step"; exit 1; }

# ────────────────────────────────────────────────────────────
# Part 1 — structural
# ────────────────────────────────────────────────────────────

# traceFormat MUST default to otel-parquet: a descriptor written before this
# param existed has to keep behaving exactly as it did.
if python3 - "${TASK}" <<'PY'
import sys, yaml
p = {x["name"]: x for x in yaml.safe_load(open(sys.argv[1]))["spec"]["params"]}
f = p.get("traceFormat")
sys.exit(0 if f and f.get("type") == "string" and f.get("default") == "otel-parquet" else 1)
PY
then pass "traceFormat param declared, default otel-parquet (back-compat)"
else fail "traceFormat param missing or does not default to otel-parquet"
fi

# traceMaxThinkTime MUST default to "" (= omit the flag). Any concrete default
# would change one of the two paths, because the converters disagree: otel caps
# at 15s, weka does not cap at all.
if python3 - "${TASK}" <<'PY'
import sys, yaml
p = {x["name"]: x for x in yaml.safe_load(open(sys.argv[1]))["spec"]["params"]}
m = p.get("traceMaxThinkTime")
sys.exit(0 if m and m.get("default") == "" else 1)
PY
then pass "traceMaxThinkTime param declared with default \"\" (flag omitted)"
else fail "traceMaxThinkTime missing or has a concrete default"
fi

# The defect the amendment named: convert hardcoded its input and never read
# the corpus-dir marker #67 introduced.
echo "${CONV}" | grep -q '/workspace/corpus_dir' \
  && pass "convert consumes the corpus_dir marker" \
  || fail "convert does not read /workspace/corpus_dir — cannot locate a weka input"

echo "${CONV}" | grep -q 'convert weka' \
  && pass "convert dispatches to 'blis convert weka'" \
  || fail "convert has no weka branch"

echo "${CONV}" | grep -q 'convert otel' \
  && pass "convert still has the otel branch" \
  || fail "convert lost the otel branch"

# min_rounds is enforced by build-otel on the otel path. build-otel is skipped
# for weka, so it must reach the converter there or the field goes inert while
# still being part of the corpus cache key.
echo "${CONV}" | grep -q -- '--min-rounds' \
  && pass "convert threads --min-rounds (weka path keeps min_rounds live)" \
  || fail "convert never passes --min-rounds — min_rounds is inert for weka"

# build-otel must be skippable, and must bail BEFORE installing pyarrow.
echo "${BO}" | grep -q '/workspace/skip_build_otel' \
  && pass "build-otel honours the skip_build_otel marker" \
  || fail "build-otel is still unconditional"

# Anchor both greps on the COMMAND at line start, not on prose: the step's
# comments mention "pip install pyarrow" and "skip_build_otel" by name, and
# matching those would compare comment positions rather than execution order.
BO_SKIP_LINE="$(printf '%s\n' "${BO}" | grep -n '^[[:space:]]*\[ -f /workspace/skip_build_otel' | head -1 | cut -d: -f1)"
BO_PIP_LINE="$(printf '%s\n' "${BO}" | grep -n '^[[:space:]]*pip install' | head -1 | cut -d: -f1)"
if [ -n "${BO_SKIP_LINE}" ] && [ -n "${BO_PIP_LINE}" ] \
   && [ "${BO_SKIP_LINE}" -lt "${BO_PIP_LINE}" ]
then pass "build-otel skips before 'pip install pyarrow'"
else fail "build-otel installs pyarrow before checking the skip marker (skip=${BO_SKIP_LINE:-none} pip=${BO_PIP_LINE:-none})"
fi

echo "${GUARD}" | grep -q 'skip_build_otel' \
  && pass "guard publishes the skip_build_otel marker" \
  || fail "guard does not set skip_build_otel"

# NOTE: the greps above are necessary but NOT sufficient, and on their own they
# are defeated by a one-token mutation — dropping the `exit 0` from build-otel's
# skip block leaves every structural assertion passing while the weka path breaks
# (it would install pyarrow, then die on "no .parquet files under ..."). The
# executable check in Part 2d is what actually pins the short-circuit.

# ────────────────────────────────────────────────────────────
# Part 2a — behavioral: the guard's format validation
# ────────────────────────────────────────────────────────────
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
mkdir -p "${TMP}/workspace/data"

# Render the guard: Tekton substitutions -> ${P_*} env refs, /workspace -> sandbox.
render() {
  printf '%s\n' "$1" \
    | sed -e "s#\$(params\.\([A-Za-z]*\))#\${P_\1}#g" \
          -e "s#\$(workspaces\.source\.path)#${TMP}/source#g" \
          -e "s#/workspace#${TMP}/workspace#g"
}
render "${GUARD}" > "${TMP}/guard.sh"

# run_guard <format> -> exit code; markers land in the sandbox.
run_guard() {
  rm -f "${TMP}/workspace/skip" "${TMP}/workspace/skip_build_otel"
  P_traceSpec="trace: {}" P_tracePath="traces/x" P_traceFormat="$1" \
    sh "${TMP}/guard.sh" > "${TMP}/guard.out" 2>&1
}

if run_guard "weka-jsonl"; then
  [ -f "${TMP}/workspace/skip_build_otel" ] \
    && pass "guard: weka-jsonl sets skip_build_otel" \
    || fail "guard: weka-jsonl did not set skip_build_otel"
  [ -f "${TMP}/workspace/skip" ] \
    && fail "guard: weka-jsonl wrongly skipped the whole task" \
    || pass "guard: weka-jsonl does not skip the task"
else
  fail "guard: weka-jsonl was rejected"; cat "${TMP}/guard.out"
fi

if run_guard "otel-parquet"; then
  [ -f "${TMP}/workspace/skip_build_otel" ] \
    && fail "guard: otel-parquet wrongly skipped build-otel" \
    || pass "guard: otel-parquet keeps build-otel enabled"
else
  fail "guard: otel-parquet was rejected"; cat "${TMP}/guard.out"
fi

# Empty => the param default applies => must behave as otel-parquet.
if run_guard ""; then
  [ -f "${TMP}/workspace/skip_build_otel" ] \
    && fail "empty traceFormat did not fall back to otel-parquet" \
    || pass "empty traceFormat falls back to otel-parquet"
else
  fail "empty traceFormat was rejected"; cat "${TMP}/guard.out"
fi

# AC4's in-pod backstop. The authoritative check is at assemble time in
# sim2real; this only has to fail rather than proceed with a bad chain.
if run_guard "weka-parquet"; then
  fail "guard accepted the unknown format 'weka-parquet'"
else
  grep -q "unknown traceFormat" "${TMP}/guard.out" \
    && pass "guard rejects an unknown format with a clear message" \
    || { fail "guard failed on unknown format but without a clear message"
         cat "${TMP}/guard.out"; }
fi

# The format check must sit AHEAD of the cache probe. The two branches are
# otherwise never combined: every case above runs with no cached pair present,
# so a guard that validated the format only after the cache check would pass all
# of them while letting a typo be masked by a cache hit — which is the specific
# thing the ordering exists to prevent.
mkdir -p "${TMP}/workspace/data/traces"
: > "${TMP}/workspace/data/traces/x.yaml"
: > "${TMP}/workspace/data/traces/x.csv"
if run_guard "weka-parquet"; then
  fail "a cache hit masked the unknown format — format check runs too late"
else
  grep -q "unknown traceFormat" "${TMP}/guard.out" \
    && pass "unknown format still rejected when the corpus is already cached" \
    || { fail "cached+unknown-format failed for the wrong reason"
         cat "${TMP}/guard.out"; }
fi
# A cache hit with a VALID format must still short-circuit the task, so the
# assertion above cannot be passing because the cache probe stopped working.
if run_guard "otel-parquet"; then
  [ -f "${TMP}/workspace/skip" ] \
    && pass "cache hit with a valid format still skips the task" \
    || fail "cache probe stopped short-circuiting"
else
  fail "guard errored on a cache hit with a valid format"; cat "${TMP}/guard.out"
fi
rm -f "${TMP}/workspace/data/traces/x.yaml" "${TMP}/workspace/data/traces/x.csv"
# The cache-hit case above left /workspace/skip behind, which every later step
# honours by design. Clear it so Parts 2b-2d exercise real code paths — a leaked
# skip marker would make them no-op, and a no-op step can pass an assertion for
# the wrong reason.
rm -f "${TMP}/workspace/skip"

# ────────────────────────────────────────────────────────────
# Part 2b — behavioral: format-driven discovery in download-corpus
# ────────────────────────────────────────────────────────────
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
then fail "could not extract the download-corpus program"; echo; echo "FAILURES"; exit 1
fi
[ -s "${TMP}/download.py" ] || { fail "extracted download program is empty"
                                 echo; echo "FAILURES"; exit 1; }

# Fake hub with one parquet dataset and one single-file JSONL dataset (the
# real weka shape: a lone traces.jsonl, not a numbered shard set).
mkdir -p "${TMP}/fake"
cat > "${TMP}/fake/huggingface_hub.py" <<'PY'
import os

LAYOUTS = {
    "Exgentic/agent-llm-traces": ("sha_p", ["data/train-00000-of-00002.parquet",
                                           "data/train-00001-of-00002.parquet",
                                           "README.md"]),
    "semianalysisai/cc-traces-weka-062126": ("sha_w", ["traces.jsonl", "README.md"]),
}


class _Info:
    def __init__(self, sha):
        self.sha = sha


class HfApi:
    def dataset_info(self, repo, revision=None):
        return _Info(LAYOUTS[repo][0])

    def list_repo_files(self, repo, repo_type=None, revision=None):
        return list(LAYOUTS[repo][1])


def hf_hub_download(repo, filename=None, repo_type=None, revision=None,
                    local_dir=None):
    dest = os.path.join(local_dir, filename)
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    with open(dest, "w") as fh:
        fh.write("stub")
    with open(os.environ["DL_LOG"], "a") as fh:
        fh.write(dest + "\n")
    return dest
PY

sed -i.bak "s#/workspace#${TMP}/workspace#g" "${TMP}/download.py"

run_dl() {
  : > "${TMP}/dl.log"
  DL_LOG="${TMP}/dl.log" PYTHONPATH="${TMP}/fake" \
  REPO="$1" REV="" SHARDS="" FORMAT="$2" \
  python3 "${TMP}/download.py" > "${TMP}/dl.out" 2>&1
}

if run_dl "hf:semianalysisai/cc-traces-weka-062126" "weka-jsonl"; then
  if grep -q 'traces\.jsonl' "${TMP}/dl.log" && ! grep -q 'README' "${TMP}/dl.log"; then
    pass "weka-jsonl discovers the .jsonl (and nothing else)"
  else
    fail "weka-jsonl fetched the wrong file set"; cat "${TMP}/dl.log"
  fi
else
  fail "weka-jsonl discovery failed"; cat "${TMP}/dl.out"
fi

if run_dl "hf:Exgentic/agent-llm-traces" "otel-parquet"; then
  n=$(grep -c 'parquet' "${TMP}/dl.log")
  [ "${n}" = "2" ] && ! grep -q 'jsonl\|README' "${TMP}/dl.log" \
    && pass "otel-parquet still discovers only .parquet" \
    || { fail "otel-parquet discovery changed"; cat "${TMP}/dl.log"; }
else
  fail "otel-parquet discovery failed"; cat "${TMP}/dl.out"
fi

# The pre-#68 failure mode, now the guard-drift assertion: asking for JSONL from
# a parquet-only dataset must fail loudly rather than fetch nothing and let a
# later step convert an empty corpus.
if run_dl "hf:Exgentic/agent-llm-traces" "weka-jsonl"; then
  fail "weka-jsonl against a parquet-only dataset silently succeeded"
else
  grep -q 'no .jsonl files found' "${TMP}/dl.out" \
    && pass "wrong-format dataset fails loudly, naming the expected extension" \
    || { fail "failure message does not name the expected extension"
         cat "${TMP}/dl.out"; }
fi

# ────────────────────────────────────────────────────────────
# Part 2c — behavioral: the convert dispatch, against a fake blis
# ────────────────────────────────────────────────────────────
mkdir -p "${TMP}/source/blis"
cat > "${TMP}/source/blis/blis" <<'SH'
#!/bin/sh
# Record argv, then produce the TraceV2 pair the step asserts on.
echo "$@" > "${ARGV_LOG}"
OUT=""
while [ $# -gt 0 ]; do
  [ "$1" = "--trace-output" ] && { OUT="$2"; break; }
  shift
done
[ -n "${OUT}" ] && { mkdir -p "$(dirname "${OUT}")"; : > "${OUT}.yaml"; : > "${OUT}.csv"; }
exit 0
SH
chmod +x "${TMP}/source/blis/blis"

render "${CONV}" > "${TMP}/convert.sh"

# run_conv <format> <maxThinkTime> <minRounds> -> argv in ${TMP}/argv.log
run_conv() {
  : > "${TMP}/argv.log"
  # Belt-and-braces: a leaked /workspace/skip from an earlier part would make the
  # step exit before any dispatch, and every argv assertion below would then be
  # checking an empty log rather than a real invocation.
  rm -f "${TMP}/workspace/skip"
  rm -f "${TMP}/workspace/data/traces/out.yaml" "${TMP}/workspace/data/traces/out.csv"
  ARGV_LOG="${TMP}/argv.log" \
  P_traceFormat="$1" P_traceMaxThinkTime="$2" P_traceMinRounds="$3" \
  P_traceContextGrowth="${4:-accumulate}" P_tracePath="traces/out" \
    sh "${TMP}/convert.sh" > "${TMP}/conv.out" 2>&1
}

# otel path: build-otel's fixed corpus.jsonl, and NO --min-rounds (build-otel
# already filtered) so today's output is unchanged.
if run_conv "otel-parquet" "" "2"; then
  ARGV="$(cat "${TMP}/argv.log")"
  case "${ARGV}" in
    "convert otel "*) pass "otel-parquet invokes 'convert otel'" ;;
    *) fail "otel-parquet invoked: ${ARGV}" ;;
  esac
  case "${ARGV}" in
    *"--input ${TMP}/workspace/data/otel/corpus.jsonl"*)
      pass "otel-parquet input is build-otel's corpus.jsonl" ;;
    *) fail "otel-parquet input wrong: ${ARGV}" ;;
  esac
  case "${ARGV}" in
    *"--context-growth accumulate"*) pass "otel-parquet passes --context-growth" ;;
    *) fail "otel-parquet dropped --context-growth: ${ARGV}" ;;
  esac
  case "${ARGV}" in
    *--min-rounds*) fail "otel-parquet passes --min-rounds (double-filters)" ;;
    *) pass "otel-parquet omits --min-rounds (build-otel owns it)" ;;
  esac
  case "${ARGV}" in
    *--max-think-time*) fail "empty traceMaxThinkTime still passed the flag" ;;
    *) pass "empty traceMaxThinkTime omits --max-think-time" ;;
  esac
else
  fail "otel-parquet convert failed"; cat "${TMP}/conv.out"
fi

# weka path: input DISCOVERED inside the keyed corpus dir, --min-rounds threaded.
CORPUS="${TMP}/workspace/data/corpus/semianalysisai_cc-traces-weka-062126@sha_w"
mkdir -p "${CORPUS}"
: > "${CORPUS}/traces.jsonl"
printf '%s' "/workspace/data/corpus/semianalysisai_cc-traces-weka-062126@sha_w" \
  | sed "s#/workspace#${TMP}/workspace#" > "${TMP}/workspace/corpus_dir"

if run_conv "weka-jsonl" "" "3"; then
  ARGV="$(cat "${TMP}/argv.log")"
  case "${ARGV}" in
    "convert weka "*) pass "weka-jsonl invokes 'convert weka'" ;;
    *) fail "weka-jsonl invoked: ${ARGV}" ;;
  esac
  case "${ARGV}" in
    *"--input ${CORPUS}/traces.jsonl"*)
      pass "weka-jsonl input discovered inside the keyed corpus dir" ;;
    *) fail "weka-jsonl input not discovered from the marker: ${ARGV}" ;;
  esac
  case "${ARGV}" in
    *"--min-rounds 3"*) pass "weka-jsonl threads --min-rounds from the descriptor" ;;
    *) fail "weka-jsonl did not thread --min-rounds: ${ARGV}" ;;
  esac
  case "${ARGV}" in
    *"--context-growth accumulate"*) pass "weka-jsonl passes --context-growth" ;;
    *) fail "weka-jsonl dropped --context-growth: ${ARGV}" ;;
  esac
else
  fail "weka-jsonl convert failed"; cat "${TMP}/conv.out"
fi

# A non-default value on BOTH paths, so the two assertions above cannot be
# satisfied by a hardcoded "accumulate" that ignores the param. context_growth is
# the highest-leverage field in the descriptor — it decides the prefix model, so a
# silently-ignored value changes what is being measured, not just a filter.
if run_conv "weka-jsonl" "" "2" "independent"; then
  grep -q -- '--context-growth independent' "${TMP}/argv.log" \
    && pass "weka-jsonl threads a non-default --context-growth" \
    || { fail "weka ignored the context-growth param"; cat "${TMP}/argv.log"; }
else
  fail "weka-jsonl convert with independent context-growth failed"; cat "${TMP}/conv.out"
fi
CORPUS_SAVED="${CORPUS}"
rm -f "${TMP}/workspace/corpus_dir"
if run_conv "otel-parquet" "" "2" "independent"; then
  grep -q -- '--context-growth independent' "${TMP}/argv.log" \
    && pass "otel-parquet threads a non-default --context-growth" \
    || { fail "otel ignored the context-growth param"; cat "${TMP}/argv.log"; }
else
  fail "otel-parquet convert with independent context-growth failed"; cat "${TMP}/conv.out"
fi
printf '%s' "${CORPUS_SAVED}" > "${TMP}/workspace/corpus_dir"

# An explicit max-think-time must reach BOTH converters — that is what lets the
# assembler stop the two defaults from diverging.
if run_conv "weka-jsonl" "15s" "2"; then
  grep -q -- '--max-think-time 15s' "${TMP}/argv.log" \
    && pass "explicit traceMaxThinkTime reaches convert weka" \
    || { fail "weka dropped --max-think-time"; cat "${TMP}/argv.log"; }
else
  fail "weka-jsonl convert with max-think-time failed"; cat "${TMP}/conv.out"
fi
rm -f "${TMP}/workspace/corpus_dir"
if run_conv "otel-parquet" "15s" "2"; then
  grep -q -- '--max-think-time 15s' "${TMP}/argv.log" \
    && pass "explicit traceMaxThinkTime reaches convert otel" \
    || { fail "otel dropped --max-think-time"; cat "${TMP}/argv.log"; }
else
  fail "otel-parquet convert with max-think-time failed"; cat "${TMP}/conv.out"
fi

# Ambiguity must fail rather than convert a subset: `convert weka` takes ONE
# JSONL, so picking one of several would silently drop corpus.
: > "${CORPUS}/extra.jsonl"
printf '%s' "${CORPUS}" > "${TMP}/workspace/corpus_dir"
if run_conv "weka-jsonl" "" "2"; then
  fail "two .jsonl files converted anyway (silently picked one)"
else
  grep -q 'expected exactly one' "${TMP}/conv.out" \
    && pass "multiple .jsonl files refused rather than silently subset" \
    || { fail "multi-jsonl failure message unclear"; cat "${TMP}/conv.out"; }
fi
rm -f "${CORPUS}/extra.jsonl"

# find's own exit status must abort the step. Plain POSIX sh has no pipefail, so
# the earlier `find | wc -l | tr -d ' '` form returned only its last command's
# status, never find's, and a find that failed
# PARTWAY (unreadable subdirectory) yielded a partial listing that looked like a
# clean result. A `[ -d ]` check does not cover it — the directory exists in this
# very scenario. Exercised with a fake find that prints one path and exits 1,
# which is the shape of a partial-traversal failure; verified by mutation that
# removing the `|| exit` makes this case pass wrongly.
printf '%s' "${CORPUS}" > "${TMP}/workspace/corpus_dir"
mkdir -p "${TMP}/bin"
cat > "${TMP}/bin/find" <<'SH'
#!/bin/sh
# One "discovered" file, then a traversal failure.
echo "${CORPUS_STUB}/partial.jsonl"
echo "find: permission denied" >&2
exit 1
SH
chmod +x "${TMP}/bin/find"
# Setup must be verified, not assumed: if this fake is missing the PATH override
# is inert, the REAL find runs, it succeeds, and the assertion below reports a
# code defect that does not exist. (That is exactly what happened while writing
# this — the bin dir did not exist yet, the redirect failed silently, and the
# case looked like a genuine failure to abort.)
[ -x "${TMP}/bin/find" ] || { fail "could not install the fake find"; }
: > "${TMP}/argv.log"
rm -f "${TMP}/workspace/skip"
if ARGV_LOG="${TMP}/argv.log" PATH="${TMP}/bin:${PATH}" CORPUS_STUB="${CORPUS}" \
   P_traceFormat="weka-jsonl" P_traceMaxThinkTime="" P_traceMinRounds="2" \
   P_traceContextGrowth="accumulate" P_tracePath="traces/out" \
     sh "${TMP}/convert.sh" > "${TMP}/conv.out" 2>&1
then
  fail "a failing find was treated as a clean single-file result"
else
  if grep -q 'find failed while scanning' "${TMP}/conv.out"; then
    if [ -s "${TMP}/argv.log" ]; then
      fail "aborted with the right message but still invoked blis"
    else
      pass "a failing find aborts before the converter, naming the real fault"
    fi
  else
    fail "failing find did not produce the find-specific message"
    cat "${TMP}/conv.out"
  fi
fi
rm -f "${TMP}/bin/find"

# A marker pointing at a directory that isn't there — the shape of a stale marker
# from a previous run, or a PVC that didn't mount. Must name the directory rather
# than blame the dataset for having no .jsonl.
printf '%s' "${TMP}/workspace/data/corpus/gone@sha" > "${TMP}/workspace/corpus_dir"
if run_conv "weka-jsonl" "" "2"; then
  fail "a corpus_dir pointing at a missing directory was accepted"
else
  grep -q 'does not exist or is not a directory' "${TMP}/conv.out" \
    && pass "stale corpus_dir naming a missing directory fails on that fact" \
    || { fail "missing-corpus-dir message blames the wrong thing"; cat "${TMP}/conv.out"; }
fi

# An empty but valid corpus dir is the legitimate zero case, and must be distinct
# from the traversal-failure and multi-file cases around it.
mkdir -p "${TMP}/workspace/data/corpus/empty@sha"
printf '%s' "${TMP}/workspace/data/corpus/empty@sha" > "${TMP}/workspace/corpus_dir"
if run_conv "weka-jsonl" "" "2"; then
  fail "an empty corpus dir was converted anyway"
else
  grep -q 'no .jsonl found under' "${TMP}/conv.out" \
    && pass "empty corpus dir reports zero .jsonl distinctly" \
    || { fail "empty-corpus message unclear"; cat "${TMP}/conv.out"; }
fi

# A missing marker is a wiring bug, not something to paper over.
rm -f "${TMP}/workspace/corpus_dir"
if run_conv "weka-jsonl" "" "2"; then
  fail "weka-jsonl succeeded with no corpus_dir marker"
else
  grep -q 'corpus_dir missing' "${TMP}/conv.out" \
    && pass "missing corpus_dir marker fails with a clear message" \
    || { fail "missing-marker message unclear"; cat "${TMP}/conv.out"; }
fi

# ────────────────────────────────────────────────────────────
# Part 2d — behavioral: build-otel's skip actually SHORT-CIRCUITS
#
# The structural greps in Part 1 cannot see whether the block exits. Removing
# just the `exit 0` keeps them all green while breaking the whole weka chain, so
# this part RUNS the step's shell wrapper with a fake `pip` on PATH and asserts
# pip is never reached. The negative case (marker absent => pip IS reached) is
# what makes the positive assertion meaningful rather than vacuous: without it, a
# step that failed for some unrelated reason before pip would also "pass".
# ────────────────────────────────────────────────────────────
mkdir -p "${TMP}/bin"
cat > "${TMP}/bin/pip" <<'SH'
#!/bin/sh
echo "pip called: $@" >> "${PIP_LOG}"
exit 0
SH
chmod +x "${TMP}/bin/pip"
# python3 must also be stubbed: past the pip line the step runs its real embedded
# program, whose failure would otherwise mask which line we actually reached.
cat > "${TMP}/bin/python3" <<'SH'
#!/bin/sh
echo "python3 called" >> "${PIP_LOG}"
exit 0
SH
chmod +x "${TMP}/bin/python3"

render "${BO}" > "${TMP}/build_otel.sh"

# run_bo <marker-present: yes|no> -> exit status; reached commands in ${TMP}/pip.log
run_bo() {
  : > "${TMP}/pip.log"
  rm -f "${TMP}/workspace/skip" "${TMP}/workspace/skip_build_otel"
  [ "$1" = "yes" ] && touch "${TMP}/workspace/skip_build_otel"
  PIP_LOG="${TMP}/pip.log" PATH="${TMP}/bin:${PATH}" \
  P_traceMinRounds="2" P_traceSplit="test" P_traceDedupByConversation="1" \
  P_traceShuffleSeed="42" \
    sh "${TMP}/build_otel.sh" > "${TMP}/bo.out" 2>&1
}

if run_bo "yes"; then
  if [ -s "${TMP}/pip.log" ]; then
    fail "build-otel did not short-circuit: reached $(cat "${TMP}/pip.log" | head -1)"
  else
    pass "build-otel with skip_build_otel exits 0 without reaching pip"
  fi
else
  fail "build-otel exited non-zero on the skip path"; cat "${TMP}/bo.out"
fi

# Counter-case: without the marker the step MUST get as far as pip. This is what
# proves the assertion above is testing the short-circuit and not an early crash.
run_bo "no"
if grep -q 'pip called' "${TMP}/pip.log"; then
  pass "build-otel without the marker does reach pip (assertion is not vacuous)"
else
  fail "build-otel never reached pip even without the marker — check the harness"
  cat "${TMP}/bo.out"
fi

echo
if ${PASS}; then echo "ALL PASS"; else echo "FAILURES"; exit 1; fi
