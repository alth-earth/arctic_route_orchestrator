---
Overall Status: ACTIVE
Content Status:
  - COMPLETED
  - PLANNED
Document Role: CANONICAL
Scope: orchestrator package README
Canonical For: orchestrator role and ownership
Branch: research-validation-system
Last Verified: 2026-08-23
Related Canonical Docs: ../arctic_route_governance/current/architecture/ARCTIC_ROUTE_SYSTEM.md
---

# A-B-C-D Root Coordinator

## Research Validation role（2026-08-21 23:18）

The Orchestrator is the Pipeline / Artifact / Presentation Adapter. It owns
replay/navigation coordination, validation and export, but not the D Viewer runtime.
The current candidate presentation package remains explicitly `NOT_PUBLISHED`; a
future versioned extension must project real C artifacts without ranking or inventing routes.

`arctic-route-orchestrator` is the root coordinator for A, B, C, and D.
It coordinates the pipeline, runs causal replay, manages navigation execution
and replan lifecycle, produces presentation artifacts, and runs L1/L2 preflight.

**Viewer implementation lives in `work_package_d/`.** The orchestrator produces
stable presentation packages (JSON/PNG) via `scripts/replay_viewer_export.py`;
D renders them. The orchestrator does NOT own HTML/JS/CSS or rendering.

Package version: `0.1.0`.

## Winter Formal Handoff（2026-08-23 01:16 +08:00）

Winter research identity `run-441b03c8-d45b-5414-b0e8-b7fd0d990c22` 已以 matching
`run-context.v2` 和 strict `orchestrator.execution-spec.v1` 发布到
`artifacts/winter-formal-handoff/`。对 active 144 小时 A bundle 的 exact archive
intake-only 已通过；这只代表 `READY_FOR_B_VALIDATION`，没有执行或发布 Winter B/C/D
结果。

Intake 的 knowledge cutoff 继续 fail closed 于任何 `issue_time > as_of_time`，但不再要求
logical cutoff 必须恰好等于 max source issue time。可见 record set 仍由 bundle IDs、
checksums、source snapshots 和 digest 精确锁定。

复验命令：

```bash
.venv/bin/arctic-route-orchestrator intake \
  --bundle ../work_package_a/data/tromso_to_isfjorden_outer_winter_20260215T000000Z_min144_bundle.json \
  --run-context artifacts/winter-formal-handoff/winter_run_context_20260215_v1.json \
  --execution-spec artifacts/winter-formal-handoff/winter_execution_spec_20260215_v1.json \
  --a-data-root ../work_package_a/data \
  --generation-id 0
```

## Presentation Export Product Contract（2026-08-20 21:13 +08:00）

The presentation adapter exports a presentation-ready current-risk projection
and Current/+6h/+12h/+24h horizon selection index from the co-located formal
`bc.risk-frame.v2` artifact. It emits explicit requested/actual valid times,
actual horizon seconds, selection method, availability, and fail-closed reason.
Future horizons use floor selection only when the requested valid time remains
inside the formal frame range; an out-of-range request never reuses the last
frame. The adapter validates frame shape and provenance and fails closed; D
does not read raw weather data or recalculate B semantics. D remains the sole
Viewer runtime owner, while this package owns the export contract and L1/L2
preflight.

The Competition Demo Final Polish export additionally publishes per-frame
presentation summaries (grid rows/columns/resolution, risk-level counts and
percentages, finite score min/max/mean, and hard-reason counts). These are
descriptive metadata only; they do not change `bc.risk-frame.v2` values,
thresholds, route semantics, or replay timing.

## Competition Demo Freeze Presentation Extension（2026-08-21 17:15 +08:00）

The export also publishes presentation-only risk forecast summary metadata
(first-to-last finite mean-score trend and explicit LAND,
`DATA_UNAVAILABLE`, and total hard-cell counts) and route display metadata
(arrival ETA and optional route metrics). Missing route metrics remain null.

`route_candidates` is exported as the backward-compatible
`presentation.route-candidates.v1` package. The current formal artifact has no
candidate geometry/metrics, so it publishes `status=NOT_PUBLISHED` with an
empty `candidates` list. D keeps the authoritative route and does not infer
Fastest/Low Risk/Recommended candidates from semantic digests.

These fields are presentation summaries only. The Orchestrator still owns
export/preflight, D remains the sole Viewer runtime owner, and no B/C/replay
semantics are recalculated or changed.

`pyrightconfig.json` is repo-local and points editor analysis at the
Orchestrator `.venv` plus `src`; it does not modify global editor settings or
production import paths. The four original export-script import diagnostics
were environment/import-resolution diagnostics for `numpy`, `geospatial`,
`preflight`, and `presentation`, not runtime failures.

## Viewer Presentation Export Polish（2026-08-21 01:20 +08:00）

`scripts/replay_viewer_export.py` now adds backward-compatible
`presentation.viewer-presentation.v1` metadata to the Viewer bundle. The
metadata makes the display boundary explicit: risk and hard layers are exact
authoritative cells with no interpolation, route densification is display-only
and collinear, and vessel position/heading are derived from the timeline and
active authoritative segment. This export metadata does not alter
`bc.risk-frame.v2`, route waypoints, ETA, adoption, or causal replay semantics.

## 当前状态

- **Demo RC1（2026-08-16）**：mur/dikson v3 四层 + 6h 重规划经 r6 首次完整 E2E、
  r7 业务级确定性复现（layer-set digest 一致）。
- 真正可中断 per-stage timeout：worker 子进程 + watchdog（`stage_worker.py` +
  `timeout_runner.py`），超时→SIGTERM→宽限→SIGKILL、无孤儿、TIMEOUT 报告；
  CLI `run` 已切换；单测覆盖。
- A* 心跳：`C_ASTAR_PROGRESS_SECONDS`（30s 示例见 r6/r7 日志）。
- 成功报告仍为 `demo_unvalidated`，并明确 `navigation_use=prohibited`。
- 验证边界：r6/r7 完整 E2E 由旧内联路径产生；RC2 已用真实 RC1 冻结 bundle 跑通
  **worker-mode full v3 E2E**（8 阶段 completed，业务结果与 r6 一致），并发现修复
  CLI 对 worker dict 结果的消费 bug；真实 C 超时冒烟 PASS
  （`scripts/real_c_timeout_worker.py`，四层 A* 45.2s 中断）。
- Demo Engineering：`scripts/demo_live_worker.py` / `demo_live_runner.py`
  提供现场真实小窗重规划（LIVE_COMPUTED，约 60s），由 D demo CLI 调用。
- Demo Candidate 2：D `demo serve` 提供 `/api/live/start` 与
  `/api/live/status`，Viewer 页面按钮可直接驱动上述 runner 并显示
  elapsed/stage 进度；正式计算仍走同一 worker/watchdog 路径。
- Route Geospatial Integrity（2026-08-17）：D 侧新增机器 gate
  （`demo geo-integrity` + preflight 硬门），审计冻结 v3 制品与风险帧的
  路线地理完整性；修复 Viewer 双投影导致的视觉穿 LAND。编排器核心路径
  无改动，正式计算仍走 worker/watchdog。
- Causal Replay Engine MVP（2026-08-18，Strategy B）：新增
  `src/arctic_route_orchestrator/replay/`（models/digests/runner/
  route_integrity/validation）+ `scripts/causal_replay_mvp.py` +
  `scripts/replay_inspect.py` + `scripts/causal_replay_preflight.py`；
  真实 Scenario B 12h/24h/44h 因果回放 PASS（engine），C 四层 =
  PLANNING-HORIZON BLOCKER（诚实 fail-closed）；Strategy A 冻结路径不受
  影响。详见 [CAUSAL_REPLAY_MVP_20260818.md](../arctic_route_governance/reports/strategy-b/CAUSAL_REPLAY_MVP_20260818.md)。
- Causal Planning Horizon Resolution（2026-08-18 第二轮）：replay /
  risk forecast / planning 三窗口解耦（77h causal forecast）；
  `--v2-only` 提供 v2 complete-route 真实规划并集成 12h 回放
  （plan_revision 1→13、route integrity PASS）；v3 four-layer =
  main_corridor contract-edge blocker（待 C 合同 proposal）。
- Strategy B Performance Hardening（2026-08-19 第四轮）：replan
  pre-planning gate（`--replan-min-interval-hours`）把 12h 从 2071.4s
  降到 1306.8s（1.59×、≈21.8min），业务轨迹 13/13 一致、determinism
  PASS；24h 扩展 1743.2s（≈29min）；Moving-vessel 字段与校验就绪。
  新增 `scripts/replay_performance_audit.py`。详见
  [STRATEGY_B_PERFORMANCE_HARDENING_20260819.md](../arctic_route_governance/reports/strategy-b/STRATEGY_B_PERFORMANCE_HARDENING_20260819.md)。
- Ship Motion Semantics（2026-08-19）：`physical_position` 与
  `planner_origin_node` 分离；`vessel_state_at(任意 t)` 提供连续船位与
  route-ETA speed；mid-edge replan 采用 next-waypoint deferred adoption，
  无瞬移。详见 [STRATEGY_B_SHIP_MOTION_SEMANTICS_20260819.md](../arctic_route_governance/reports/strategy-b/STRATEGY_B_SHIP_MOTION_SEMANTICS_20260819.md)。

## 接手顺序

1. [编排器项目交接](arctic_route_orchestrator_handoff.md)
2. [长集成运行事故复盘](docs/INCIDENT_2026-08-14_LONG_INTEGRATION_RUN.md)
3. [系统整体架构](../arctic_route_governance/current/architecture/ARCTIC_ROUTE_SYSTEM.md)
4. [十日计划(archived)](../arctic_route_governance/archive/superseded/ABC_10_DAY_SPRINT.md)
5. [版本记录](CHANGELOG.md)

历史短版入口保存在
[治理前 README](README.archive-20260814-pre-governance.md)，只用于追溯。

## 标准环境门禁

```bash
cd /root/my_project/arctic_route_orchestrator
make env-create
make sync
make check
```

若首次同步时构建隔离无法取得 hatchling 元数据，可先在该项目 `.venv` 安装已缓存的
`hatchling`/`editables`，再执行 `UV_NO_BUILD_ISOLATION=1 make sync`；这不是改用其他项目
环境，本次修复已按此方式完成同步。

环境门禁修复后仍须分阶段运行 v2、重规划和 v3；不要直接重启无心跳、无阶段超时的完整
长用例。

编排器不拥有正式环境数据或模型权重；运行时通过 `--bundle/--run-context/--a-data-root/
--b-config/--c-config-root/--contracts-config-root/--risk-store-root/--output-dir` 参数
引用外部制品（源自：arctic_route_orchestrator_handoff_归档_20260815.md）。
