#!/usr/bin/env bash
# M2K short-baseline retest FINALIZE (2026-08-27).
# Waits for the short retest master (run_m2k_short_baseline.sh) to exit, then
# extracts the definitive numbers from baseline/manifest.json and writes a
# verdict summary to /tmp/m2k_short_summary.txt for the agent to pick up.
#
# Waits up to 70 minutes (poll 30s). Idempotent: if summary already exists and
# is final, exits immediately.
set -u
set -o pipefail

ROOT=/root/my_project/.runtime/experiments/winter-c-p21-m2k-short-baseline-20260827-r1
VENV=/root/my_project/arctic_route_orchestrator/.venv/bin/python
MASTER_PID="${1:-1613510}"
SUMMARY=/tmp/m2k_short_summary.txt
MANIFEST="$ROOT/baseline/manifest.json"
MAX_WAIT_MIN=70

if [ -s "$SUMMARY" ] && grep -q "FINAL_VERDICT" "$SUMMARY"; then
  echo "summary already final; exiting" >&2
  exit 0
fi

deadline=$(( $(date +%s) + MAX_WAIT_MIN*60 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  if ! kill -0 "$MASTER_PID" 2>/dev/null; then
    # master gone: give manifest a moment to settle, then extract
    sleep 5
    if [ -f "$MANIFEST" ]; then
      echo "== M2K SHORT RETEST FINALIZE $(date -u +%FT%TZ) ==" > "$SUMMARY"
      "$VENV" - "$MANIFEST" >> "$SUMMARY" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
d = m.get("diagnostic_summary", {})
fcb = d.get("focus_cell_by_order", {}).get("rolling_0_24h::fastest", {})
cf = fcb.get("candidate-first", {}).get("median_regression_percent")
co = fcb.get("control-first", {}).get("median_regression_percent")
ov = d.get("focus_cell_overall", {}).get("rolling_0_24h::fastest", {}).get("median_regression_percent")
print(f"status: {m.get('status')}")
print(f"runner_failures: {m.get('runner_failures')}  passed_cases: {m.get('passed_cases')}")
print(f"execution_order_counts: {d.get('execution_order_counts')}")
print(f"gates: {d.get('gates')}")
print(f"max_order_gap_percent_points: {d.get('max_order_gap_percent_points')}")
print(f"rolling_0_24h::fastest  candidate-first median_regression%: {cf}")
print(f"rolling_0_24h::fastest  control-first   median_regression%: {co}")
print(f"rolling_0_24h::fastest  overall         median_regression%: {ov}")
try:
    v = float(cf)
    verdict = "CONVERGED" if -5.0 <= v <= 5.0 else "OUTLIER"
except (TypeError, ValueError):
    verdict = "UNKNOWN"
print(f"VERDICT candidate-first: {verdict} (target: within [-5%, +5%])")
print(f"FINAL_VERDICT: {verdict}")
PY
      echo "finalized rc=$?" >> "$SUMMARY"
      echo "FINALIZE done -> $SUMMARY"
      exit 0
    else
      echo "master gone but manifest missing at $(date -u +%FT%TZ)" > "$SUMMARY"
      exit 1
    fi
  fi
  sleep 30
done
echo "TIMEOUT: master still alive after ${MAX_WAIT_MIN}min at $(date -u +%FT%TZ)" > "$SUMMARY"
exit 2
