#!/bin/sh
# Tests the awk log-splitting logic used in collect-results (collect-epp-logs step).
# Run with: sh tektonc-data-collection/tests/test_stream_epp_logs_awk.sh

TMPDIR_TEST=$(mktemp -d)
trap "rm -rf ${TMPDIR_TEST}" EXIT

PASS=true

assert_file_exists() {
  if [ ! -f "$1" ]; then
    echo "FAIL: $1 not found"
    PASS=false
  else
    echo "PASS: $1 exists"
  fi
}

assert_line_count() {
  COUNT=$(wc -l < "$1" | tr -d ' ')
  if [ "${COUNT}" -eq "$2" ]; then
    echo "PASS: $(basename $1) has $2 lines"
  else
    echo "FAIL: $(basename $1) has ${COUNT} lines, expected $2"
    PASS=false
  fi
}

# --- Input: kubectl logs --timestamps=true format ---
# Format: <RFC3339-timestamp> <original-json-log-line>
# lines 1+2 → bucket 2325 (floor(28/5)*5=25, floor(29/5)*5=25)
# lines 3+4 → bucket 2330 (floor(30/5)*5=30, floor(34/5)*5=30)
# line 5    → bucket 2335 (floor(35/5)*5=35)
# line 6    → bucket 2325 (contains spaces in JSON value — must be preserved)
cat > "${TMPDIR_TEST}/input.txt" << 'EOF'
2026-04-11T23:28:39.123456789Z {"level":"Level(-5)","ts":"2026-04-11T23:28:39Z","msg":"line 1"}
2026-04-11T23:29:59.999999999Z {"level":"Level(-5)","ts":"2026-04-11T23:29:59Z","msg":"line 2"}
2026-04-11T23:30:00.000000000Z {"level":"Level(-5)","ts":"2026-04-11T23:30:00Z","msg":"line 3"}
2026-04-11T23:34:59.000000000Z {"level":"Level(-5)","ts":"2026-04-11T23:34:59Z","msg":"line 4"}
2026-04-11T23:35:00.000000000Z {"level":"Level(-5)","ts":"2026-04-11T23:35:00Z","msg":"line 5"}
2026-04-11T23:28:50.000000000Z {"level":"info","msg":"a message with spaces","key":"val"}
EOF

POD="test-pod-abc123"
WIN=5
OUTDIR="${TMPDIR_TEST}/epp_logs"
mkdir -p "${OUTDIR}"

# --- The awk script (copied verbatim from collect-results.yaml collect-epp-logs step) ---
awk -v outdir="${OUTDIR}" -v pod="${POD}" -v win="${WIN}" '
  {
    split($1, dt, "T")
    split(dt[2], tm, ":")
    hour = tm[1]
    min  = int(tm[2])
    bucket = int(min / win) * win
    fname = sprintf("%s/%s_%s%02d.log", outdir, pod, hour, bucket)
    out = ""
    for (i = 2; i <= NF; i++) out = out (i > 2 ? " " : "") $i
    print out >> fname
    fflush(fname)
  }
' < "${TMPDIR_TEST}/input.txt"

# --- Assertions ---

FILE_2325="${OUTDIR}/${POD}_2325.log"
FILE_2330="${OUTDIR}/${POD}_2330.log"
FILE_2335="${OUTDIR}/${POD}_2335.log"

assert_file_exists "${FILE_2325}"
assert_file_exists "${FILE_2330}"
assert_file_exists "${FILE_2335}"

assert_line_count "${FILE_2325}" 3   # lines 1, 2, 6
assert_line_count "${FILE_2330}" 2   # lines 3, 4
assert_line_count "${FILE_2335}" 1   # line 5

# Output must be pure JSON — first char must be '{'
FIRST_CHAR=$(head -c1 "${FILE_2325}")
if [ "${FIRST_CHAR}" = "{" ]; then
  echo "PASS: output is pure JSON (no kubectl timestamp prefix)"
else
  echo "FAIL: unexpected first char '${FIRST_CHAR}', expected '{'"
  PASS=false
fi

# Spaces within JSON values must be preserved
if grep -q '"a message with spaces"' "${FILE_2325}"; then
  echo "PASS: spaces in JSON values preserved"
else
  echo "FAIL: spaces in JSON values not preserved"
  PASS=false
fi

# Files must sort lexicographically in chronological order
SORTED=$(ls "${OUTDIR}" | sort)
EXPECTED="${POD}_2325.log
${POD}_2330.log
${POD}_2335.log"
if [ "${SORTED}" = "${EXPECTED}" ]; then
  echo "PASS: files sort lexicographically = chronologically"
else
  echo "FAIL: sort order unexpected: ${SORTED}"
  PASS=false
fi

echo ""
if ${PASS}; then
  echo "ALL TESTS PASSED"
  exit 0
else
  echo "SOME TESTS FAILED"
  exit 1
fi
