#!/usr/bin/env bash
# M2K short-baseline smoke (2026-08-27). Verification-only run to check whether
# the candidate-first anomaly (+25.28% in M2K baseline, driven by a single
# case-004 +47.69% wall-clock outlier) reproduces under the same symmetric
# warm-up. Fewer repetitions keep it short; result is evidence, not a formal gate.
#
# baseline only = --isolation per-track --diagnostic-profile baseline --warmup-runs 1
#   --repetitions 3  (alternate order => candidate-first n=2, control-first n=1)
#
# This does NOT relax any frozen ceiling; gate_verdict is still computed, but the
# run is a noise probe (repetitions < _M2_MIN_REPETITIONS=3 means screening/auto).
set -u
set -o pipefail

ORCH=/root/my_project/arctic_route_orchestrator
RS=/root/my_project/.runtime/experiments/winter-b-validation-holdout-total-20260826/risk-store
COMMIT=$RS/commits/risk-window-sha256-115ad3ab6d7034fabc9428f91c14099b02dff8bb2443569a8d3947187fbb5ff9.json
RC=/root/my_project/.runtime/experiments/a-winter-formal-holdout-total-20260222/run_context.json
ES=/root/my_project/.runtime/experiments/a-winter-formal-holdout-total-20260222/execution_spec.json
ROOT=/root/my_project/.runtime/experiments/winter-c-p21-m2k-short-baseline-20260827-r1
VENV=$ORCH/.venv/bin/python

COMMON="--candidate-mode control-trace --evidence-mode diagnostic --rss-mode isolated --repetitions 3 --warmup-runs 1"
COMMON="$COMMON --risk-store-root $RS --risk-commit $COMMIT --run-context $RC --execution-spec $ES"

cd "$ORCH" || exit 1

OUT="$ROOT/baseline"
LOG="$ROOT/baseline.log"
rm -rf "$OUT"; mkdir -p "$OUT"
echo "===== M2K SHORT baseline START $(date -u +%FT%TZ) =====" | tee -a "$LOG"
"$VENV" scripts/winter_p2_shadow.py $COMMON \
  --isolation per-track --diagnostic-profile baseline --output-dir "$OUT" 2>&1 | tee -a "$LOG"
rc=${PIPESTATUS[0]}
echo "===== M2K SHORT baseline END $(date -u +%FT%TZ) rc=$rc =====" | tee -a "$LOG"
exit $rc
