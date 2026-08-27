#!/usr/bin/env bash
# Watch the M2J retest and alert (bell + banner) when both baseline and
# treatment manifests are produced. Also extracts the key cell
# `rolling_0_24h x fastest` median_regression_percent from each manifest.
#
# Usage:
#   bash scripts/watch_m2j_retest.sh [ROOT] [poll_seconds] [max_wait_minutes]
#   default ROOT=M2J dir, poll=60s, max_wait=240min.  Ctrl-C to stop early.
#   M2K example:  bash scripts/watch_m2j_retest.sh \
#       /root/my_project/.runtime/experiments/winter-c-p21-m2k-symmetric-warmup-20260827-r1
set -u
ROOT=${1:-/root/my_project/.runtime/experiments/winter-c-p21-m2j-measurement-protocol-20260827-r1}
POLL=${2:-60}
MAXWAIT=${3:-240}
MASTER=$ROOT/master.log
BASENAME=baseline
TREATNAME=treatment

# Extract from diagnostic_summary (M2J diagnostic mode). Avoid '%' inside an
# f-string expression (breaks Python 3.12 'unexpected character after line
# continuation character'); build messages via string concatenation instead.
PY_EXTRACT='import json,sys
p=sys.argv[1]
try:
    m=json.load(open(p,encoding="utf-8"))
except Exception as e:
    print("  (manifest unreadable: %s)" % (e,)); sys.exit(0)
status=m.get("status","?")
print("  status=%s" % (status,))
print("  diagnostic_profile=%s" % (m.get("diagnostic_profile","?"),))
ds=m.get("diagnostic_summary") or {}
print("  gate_verdict=%s  formal_gate_verdict=%s" % (ds.get("gate_verdict","?"), ds.get("formal_gate_verdict","?")))
gates=ds.get("gates") or {}
print("  gates: order_gap_le_5pp=%s order_median_reg_le_5=%s overall_median_reg_le_3=%s" % (
    gates.get("focus_cell_order_gap_le_5pp","?"),
    gates.get("focus_cell_order_median_regression_le_5_percent","?"),
    gates.get("focus_cell_overall_median_regression_le_3_percent","?"),
))
byorder=ds.get("focus_cell_by_order") or {}
cell=byorder.get("rolling_0_24h::fastest") or {}
for ord_key in ("candidate-first","control-first"):
    v=cell.get(ord_key) or {}
    med=v.get("median_regression_percent")
    if med is not None:
        print("  ** rolling_0_24h x fastest [%s]: median_regression%%=%s p95_regression%%=%s (n=%s)" % (
            ord_key, med, v.get("p95_regression_percent"), v.get("sample_count")))
os=ds.get("order_stratified") or {}
ov=ds.get("focus_cell_overall") or {}
fr=ov.get("rolling_0_24h::fastest") or {}
if fr.get("median_regression_percent") is not None:
    print("  focus_cell_overall rolling_0_24h x fastest: median_regression%%=%s (n=%s)" % (
        fr.get("median_regression_percent"), fr.get("sample_count")))
print("  max_order_gap_percent_points=%s" % (os.get("max_order_gap_percent_points"),))'

elapsed=0
echo "Watching M2J retest in: $ROOT"
echo "  poll=${POLL}s  max_wait=${MAXWAIT}min  (Ctrl-C to stop)"
while true; do
  b_done=$( [ -f "$ROOT/$BASENAME/manifest.json" ] && echo 1 || echo 0 )
  t_done=$( [ -f "$ROOT/$TREATNAME/manifest.json" ] && echo 1 || echo 0 )
  alive=$( pgrep -f "winter_p2_shadow" >/dev/null && echo 1 || echo 0 )
  alldone=$( grep -q "ALL DONE" "$MASTER" 2>/dev/null && echo 1 || echo 0 )

  if [ "$alldone" = 1 ] || { [ "$b_done" = 1 ] && [ "$t_done" = 1 ] && [ "$alive" = 0 ]; }; then
    echo
    echo "==================================================================="
    echo "  M2J RETEST COMPLETE  ($(date -u +%FT%TZ))"
    echo "==================================================================="
    for n in "$BASENAME" "$TREATNAME"; do
      echo "--- $n ---"
      if [ -f "$ROOT/$n/manifest.json" ]; then
        python3 -c "$PY_EXTRACT" "$ROOT/$n/manifest.json"
      else
        echo "  (no manifest.json)"
      fi
    done
    echo "==================================================================="
    echo "Compare: baseline rolling_0_24h x fastest median_regression% should be >5 (FAIL);"
    echo "         treatment should be <=5 (PASS) => 5.94% is measurement artifact."
    echo "==================================================================="
    printf '\a'
    exit 0
  fi

  stage="baseline running"
  [ "$b_done" = 1 ] && stage="treatment running"
  printf '\r[%s] elapsed=%dm alive=%s baseline_manifest=%s treatment_manifest=%s stage=%s' \
    "$(date -u +%H:%M:%S)" "$((elapsed/60))" "$alive" "$b_done" "$t_done" "$stage"
  sleep "$POLL"
  elapsed=$((elapsed+POLL))
  if [ "$((elapsed/60))" -ge "$MAXWAIT" ]; then
    echo
    echo "WATCH TIMEOUT after ${MAXWAIT}min (still running). Re-run to keep watching."
    exit 1
  fi
done
