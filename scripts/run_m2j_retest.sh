#!/usr/bin/env bash
# M2J measurement-protocol retest (corrected design, 2026-08-27).
# baseline  = --isolation per-track   --diagnostic-profile baseline  (retains full_voyage trace -> reproduces ~5.94% FAIL)
# treatment = --isolation per-unit-phase                           (R1 forces candidate=trace-release-only -> R2 release)
# Runs sequentially (controlled host; no concurrent CPU/RSS contamination).
set -u
set -o pipefail

ORCH=/root/my_project/arctic_route_orchestrator
RS=/root/my_project/.runtime/experiments/winter-b-validation-holdout-total-20260826/risk-store
COMMIT=$RS/commits/risk-window-sha256-115ad3ab6d7034fabc9428f91c14099b02dff8bb2443569a8d3947187fbb5ff9.json
RC=/root/my_project/.runtime/experiments/a-winter-formal-holdout-total-20260222/run_context.json
ES=/root/my_project/.runtime/experiments/a-winter-formal-holdout-total-20260222/execution_spec.json
ROOT=/root/my_project/.runtime/experiments/winter-c-p21-m2j-measurement-protocol-20260827-r1

COMMON="--candidate-mode control-trace --evidence-mode diagnostic --rss-mode isolated --repetitions 5"
COMMON="$COMMON --risk-store-root $RS --risk-commit $COMMIT --run-context $RC --execution-spec $ES"

cd "$ORCH" || exit 1

run_one () {
  local name="$1"; local iso="$2"; local out="$3"; local log="$4"
  rm -rf "$out"; mkdir -p "$out"
  echo "===== M2J $name START $(date -u +%FT%TZ) =====" | tee -a "$log"
  uv run python scripts/winter_p2_shadow.py $COMMON \
    --isolation "$iso" --diagnostic-profile baseline --output-dir "$out" 2>&1 | tee -a "$log"
  local rc=${PIPESTATUS[0]}
  echo "===== M2J $name END $(date -u +%FT%TZ) rc=$rc =====" | tee -a "$log"
  return $rc
}

run_one baseline   per-track      "$ROOT/baseline"   "$ROOT/baseline.log"
base_rc=$?
run_one treatment  per-unit-phase "$ROOT/treatment"  "$ROOT/treatment.log"
treat_rc=$?

echo "===== M2J ALL DONE base_rc=$base_rc treat_rc=$treat_rc $(date -u +%FT%TZ) =====" | tee -a "$ROOT/run.log"
