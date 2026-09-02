---
Overall Status: ACTIVE
Content Status:
  - COMPLETED
  - IN_PROGRESS
Document Role: SUPPORTING
Scope: orchestrator change history
Branch: research-validation-system
Last Verified: 2026-09-02
---

# Changelog

## Unreleased - arrival-complete replay and formal-motion binding（2026-09-02）

- replay/export: restore the original frozen Winter identity and publish the complete 144-hour
  retrospective run with 25 snapshots, 9 immutable plan revisions, 119 observed events and a
  terminal `ARRIVED` state; all eight decided replans have adopted/route-changed evidence;
- presentation: overlay exact event-time adoption state between sparse snapshots, keep completed
  track append-only, clear the current segment only at arrival, and transport all eight revision
  transitions plus all nine revision candidate/motion sets;
- formal motion: preserve per-revision adoption offsets; D now tolerates only the sub-millisecond
  precision necessarily lost by JavaScript `Date`, while still rejecting a 2 ms mismatch;
- evidence: RC2 objective parallelism remained active (`3/3/3`, 36 planning calls, 108 tasks),
  R1–R4 use qualified curves, R5–R7 fail closed on increased integrated risk, and R8–R9 fail
  closed on geometric eligibility; the Viewer assembly has 8,641 minute samples.
- explanation: do not attach the later holdout sidecar to the restored identity; its exact A source
  trace was retired, so the optional consumer remains honestly unavailable without affecting risk,
  routing, or replay.

## Unreleased - Winter dynamic replay and RC2 objective parallelism（2026-09-01 22:40 +08:00）

- replay: publish real Winter `retrospective_dynamic_replay` resources with revision-level
  immutable 4-layer/12-route plan sets and observed `REPLAN_DECIDED` / `REPLAN_ADOPTED` events;
  the temporal claim remains explicitly post-hoc, not causal;
- perf: restore RC2 three-worker `ProcessPool` objective parallelism in formal C initial and
  replanning calls, with fail-closed worker/pool validation and run-report telemetry;
- safety: keep ticks, layers, B, switch/adoption gates, route metrics, ETA and hard constraints
  serial/authoritative; B-spline remains presentation-only and cannot alter route semantics;
- test: add parallel install/profile regression coverage and preserve B's expected
  `allowed_region_has_no_grid_node` fail-closed endpoint test.
- integration: bind B's real same-call `risk-explanation.v1` artifact/manifest to the Winter
  RiskWindow with deterministic `generated_at`; D receives the content-addressed sidecar and
  preserves producer `COMPLETE/PARTIAL/UNAVAILABLE` semantics.

## Unreleased - identity-bound replay and risk explanation transport（历史基线，2026-09-01）

> 后续当前状态：默认 Winter 路径已显式采用真实 `retrospective_dynamic_replay`；strict
> `causal_replay` 仍按 issue-time 门禁失败关闭。本节 sidecar PASS 属于中间 holdout 身份；
> 当前恢复的原始冻结身份没有可重建 trace，详见顶部增量与治理运行报告。

- Winter export accepts only a strict `causal_replay` manifest with matching
  DatasetBundle/RunContext/RiskWindow/layer identity; real observed
  `REPLAN_DECIDED` + `REPLAN_ADOPTED`, multiple revisions and route state are consumed
  verbatim. Without that source, the default export publishes no synthetic events and an
  explicit unavailable replanning status.
- `--risk-explanation-manifest` transports B's immutable sidecar only after digest/schema/
  identity verification; Orchestrator never reconstructs contributors.
- Existing route, risk, ETA, and Viewer fallback semantics remain unchanged; no Winter causal
  replay is claimed until a matching source artifact is supplied.

## Unreleased - Formal route motion default transport（2026-08-31）

- production/default Winter export now requires a validated `cd.route-motion-set.v1`
  via `--require-route-motion`; the preflight records the formal-motion gate and the
  immutable manifest records whether the requirement was active;
- immutable Winter packages now carry the bound four-layer plan set and formal motion
  JSON as checksum-covered transport files;
- historical research smoothing sidecars remain export-compatible for explicit research
  use but are not a production fallback;
- no RoutePlan, ETA, risk, R1/R2, or historical sidecar artifact was rewritten.

## Unreleased - Route display smoothing（2026-08-30 23:38 +08:00）

- presentation: `presentation.viewer-presentation.v1` declares constrained local
  cubic B-spline paint geometry for planned route lines and authoritative-polyline
  linear densification as its fail-closed fallback；不改变 waypoint、ETA、metrics、
  risk、adoption 或权威 vessel-motion 语义；D Viewer 可在本地以原始 ETA 时间锚点让
  仿真船位/航向/轨迹沿已接受曲线绘制，不写回 bundle，也不构成生产运动资格；

## Unreleased - Winter Combined Viewer Assembly（2026-08-23 20:14 +08:00）

- feat: 为 `replay_viewer_export.py` 增加 Winter combined assembly，严格绑定
  DatasetBundle、RunContext、145-frame committed RiskWindow、C v3 plan set、12-route
  candidate sidecar 与 integrity evidence；
- feat: 从 C full-voyage recommended waypoint ETA 投影 1-minute navigation simulation
  timeline，并明确标记 `source_replay=null`，不伪称 causal replay；
- safety: scenario/run/bundle/risk-window/candidate/selected-route/geometry/metrics 任一不一致
  都 fail closed；不调用或修改 A/B/C；
- compatibility: 原 causal-replay export 路径和 D single-route fallback 保持不变。

## Unreleased - Winter C 与 Route Presentation 接口（2026-08-23 10:20 +08:00）

- 新增只消费已提交 `bc.risk-frame.v2` 窗口的 Winter C validation runner；不重跑 A/B；
- 新增 `presentation.route-candidates.v1` 严格投影、双状态 schema 与 fail-closed loader，
  将 C 的 4 层 × 3 目标完整复制为 12-route sidecar，不重排、不重算；
- `replay_viewer_export.py` 可选接收已验证 sidecar，并拒绝 scenario identity 不一致；
- 既有 bundle 未提供 sidecar 时继续发布 `NOT_PUBLISHED`，保持向后兼容。

## Unreleased - Winter Formal Handoff（2026-08-23 01:16 +08:00）

- publish matching `run-context.v2` and strict `orchestrator.execution-spec.v1`
  artifacts for the frozen Winter DatasetBundle;
- align intake knowledge-cutoff validation with the shared temporal invariant
  `max(record.issue_time) <= bundle.as_of_time`; future-issued records remain
  fail closed;
- validate the exact 1,212-record archive through intake-only without invoking
  B RiskBuild, C planning, replay, or D Viewer;
- no bundle, contract schema, B/C algorithm, route, ETA, or Viewer semantics changed.

## Unreleased - Competition Demo Freeze Presentation Extension（2026-08-21 17:15 +08:00）

- presentation export: publish forecast trend and explicit hard-cell counts
  from already exported risk-frame summaries;
- presentation export: publish route arrival ETA, optional existing metrics,
  and an empty backward-compatible `presentation.route-candidates.v1` package
  when candidate geometry/metrics are absent;
- preserve all authoritative B risk, C route, ETA, adoption, replay, and
  determinism semantics.

## Unreleased - Competition Demo Final Polish（2026-08-21 15:30 +08:00）

- presentation export: add per-frame risk grid metadata and distribution
  summaries (counts, percentages, finite score min/max/mean, hard-reason
  counts) as a backward-compatible Viewer presentation extension;
- preserve `bc.risk-frame.v2`, horizon selection, route, ETA, adoption, and
  causal replay semantics; D consumes the exported summaries only for display
  and audit.

## Unreleased - Viewer Presentation Polish（2026-08-21 01:20 +08:00）

- feat: export backward-compatible `presentation.viewer-presentation.v1`
  metadata describing exact risk/hard cell rendering, display-only collinear
  route densification, and ETA/segment-bearing vessel rendering;
- test: add contract assertions for the display-only policies; Orchestrator
  fast suite passes 78 tests with 2 integration tests deselected and ruff clean;
- no authoritative risk, route, ETA, adoption, causal replay, or determinism
  semantics changed.

## Unreleased - 2026-08-20（Viewer ownership migration）

- fix: add repo-local `pyrightconfig.json` for the Orchestrator `.venv` and
  `src` resolution; the four export-script editor import diagnostics are now
  closed without a production `sys.path` workaround;
- feat: add fail-closed Current/+6h/+12h/+24h horizon selection metadata to
  `presentation.risk-overlay.v1`, including requested/actual valid time,
  actual horizon seconds, selection method, availability, reason, and frame
  index;
- feat: export a presentation-ready `presentation.risk-overlay.v1` current
  risk projection from formal `bc.risk-frame.v2` frames; preserve
  `valid_time`, risk levels, confidence, provenance, and separate hard
  reasons without recomputing B semantics in D;
- fix: expose superseded future-route segments and use the actual
  `REPLAN_ADOPTED` event as the effective adoption time when present;

- refactor: hand off viewer presentation runtime to work_package_d;
  D now owns HTML/JS/CSS, Simulation Clock, moving ship, static server,
  proof renderer;
- added `scripts/replay_viewer_export.py`: backend artifact producer
  (bundle.json, gebco_basemap.png, basemap_metadata.json,
  replay-viewer-preflight.json) — orchestrator owns presentation package
  production, D renders;
- removed `viewer/build_basemap.py`, `viewer/build_bundle.py`,
  `viewer/embed.py`, `viewer/render_proof.py`, `viewer/README.md`,
  `scripts/replay_viewer_serve.py`, `tests/unit/test_viewer_bundle.py`
  (migrated to D);
- fixed basemap export to write proper PNG via `_write_png` (was writing
  raw RGB bytes);


## Unreleased - 2026-08-19（Replay-driven Viewer MVP + L2 Preflight）

- `replay/geospatial.py`：L2 gate 改为 raster-cell traversal（mask grid，
  oversample<=2x cell，out-of-bounds fail-closed）；修正 GEBCO
  `land_sea_mask` 极性（规范 `1=sea,0=land_or_coast`）；
- `replay/preflight.py` + `scripts/replay_l2_preflight.py` +
  `scripts/replay_viewer_preflight.py`：L2 GEBCO coastline + artifacts +
  canonical EPSG:4326 transform + layer coverage → `presentation_eligible`；
  真实 Scenario B 12h：5 route revisions + completed tracks 全 PASS；
- `replay/presentation.py`：`PresentationPlan` 新增显式
  `active_plan_revision / pending_plan_revision / pending_plan_status`；
- `viewer/`：build_basemap（纯 Python PNG）、build_bundle（Presentation
  Adapter 生成 1min timeline）、embed（无 server 单文件）、render_proof
  （数据 proof PNG）、index/app/style（Simulation Clock/Play/Pause/scrub/
  moving ship/pending route/debug）；
- `scripts/replay_viewer_serve.py`：离线 127.0.0.1 静态+`/api/state?t=`；
- tests：`test_replay_preflight.py` + `test_viewer_bundle.py`；unit 78 passed；

## Unreleased - 2026-08-19（Strategy B Viewer Backend Baseline）

- 新增 `replay/presentation.py`：Presentation Adapter（manifest + snapshots →
  stable presentation contract）；`state_at(t)` / `vessel_at(t)` 支持任意
  仿真时刻船位（accepted route ETA + `vessel_state_at`，snapshot cadence ≠
  render cadence）；`adoption_audit()` 输出机器可读
  IMMEDIATE / NEXT_WAYPOINT_DEFERRED 决策、snap、revisions、track 长度；
- 新增 `scripts/replay_presentation.py` CLI（`--audit` / `--state`）；
- NavigationExecutionState 新增 `accepted_route` / `pending_route` /
  `superseded_route` / `planner_origin_position`（physical-clock ETA），
  Adapter 不重算 Planner、不发明航速；Runner 统计修正：`REPLAN_DECIDED`
  计入 candidate_computed / candidate_accepted；
- 新增 `replay/geospatial.py`：EPSG:4326 canonical transform、basemap
  metadata、L2 coastline gate harness + 本地 GEBCO_2026 land_sea_mask
  real-data smoke；
- 最新 HEAD 12h authoritative（`sb-viewer-baseline-12h(-det)`）：13
  snapshots、2044.9s、validation / route integrity PASS、determinism PASS
  （manifest + 13/13 snapshot + risk + route digest 一致）；
- tests：`test_replay_presentation.py`（T1-T7 + audit + 旧 schema 拒绝）+
  `test_replay_geospatial.py`；unit 70 passed；ruff 全绿。

## Unreleased - 2026-08-19（Strategy B Ship Motion Semantics）

- 新增纯 kinematics `replay/vessel_motion.py`：`vessel_state_at(任意 t)` 输出
  连续 physical position、edge progress、segment ETAs、speed_mps/knots、
  course、executed/remaining、NOT_STARTED/UNDERWAY/ARRIVED；route ETA 严格
  递增否则 fail-closed；
- `physical_position` 与 `planner_origin_node` 分离：planner snap 不再写回
  物理船位；NavigationExecutionState 新增 planner_origin_node/adjustment、
  replan_decision_time、effective_adoption_time、adoption_status、
  candidate_plan_revision、replan_physical_position；
- replan next-waypoint deferred adoption：mid-edge 决策时物理船继续当前
  accepted route，新路线在下一执行节点 `REPLAN_ADOPTED` 生效，避免 planner
  snap 造成瞬移；IMMEDIATE 仍用于 exactly-waypoint 决策；
- 真实 12h artifact audit：speed≈9.65 knots、1h 位移≈20.4km、snap
  median≈5.1km / max≈17.9km、grid edge median≈40.8km；
- tests：`test_vessel_motion.py`（T1-T6 + 5min 高频采样）+ navigation
  deferred no-teleport；unit 42 项 PASS；ruff 全绿。

## Unreleased - 2026-08-19（Strategy B Performance Hardening + Moving-Vessel Semantics）

- Replan 成本审计：旧 12h = 13 candidate、6 accepted、7 rejected
  （rejected ≈52% candidate 计算时间）；新增
  `scripts/replay_performance_audit.py` 逐 tick 输出 candidate/accepted/
  rejected/pre-gate skip compute 时间与船位/snap 统计；
- Pre-planning gate（interval 版）：`--replan-min-interval-hours=2` 仅在
  A/B 内容无变化且 accepted plan 未达间隔时跳过 C，任何
  DATA/RISK-content 事件无条件放行（fail-closed，不替代 Switch Gate）；
- 真实 12h 权威（v3 × 3 objectives、3 workers）：1306.8s（约 21.8min），
  旧 2071.4s → speedup 1.59×，节省约 12.7min；C candidate 13→8、
  rejected 7→2、pre-gate skip 5；业务轨迹与旧 12h 13/13 逐一一致
  （plan_revision 1→6、route/risk digest 全等）；
- Moving-vessel 语义：`NavigationExecutionState` 新增 current_edge_index、
  current_segment_start/end_eta、effective_speed_knots、speed_source、
  executed_distance_km、cumulative_travelled_km；validation 增加
  stationary-vessel / cumulative 单调检查；
- Determinism：独立输出根复跑（1264.8s），manifest semantic digest、
  13/13 snapshot digest、risk/route digest 全部一致；wall-clock 允许不同；
- 24h 扩展（optional）：1743.2s（约 29min）、25 snapshots、candidate 14、
  accepted 12、rejected 2、skip 11、plan_revision 1→12、validation PASS，
  12h 前缀与权威 12h 13/13 一致；
- Persistent-pool + worker component cache 实验：单次 1-tick benchmark
  percall 167.1s vs persistent 167.5s（无实质收益），默认保留 per-call，
  persistent 作为显式 opt-in，避免无谓复杂度；
- Tests：replay unit 34 项、C 142 项、ruff 全绿；RC1/RC2 frozen regression
  仍 PASS。

## Unreleased - 2026-08-18 第三轮（Strategy B Semantic Hardening）

- Revision 语义拆分：`data_revision` / `b_input_revision` /
  `risk_content_revision` / `risk_window_revision` / `observation_sequence` /
  `plan_revision` / `navigation_state_revision` 各自独立；replan reason
  诚实翻译（observation_sequence 不再冒充 data_revision，suffix window
  前移不再误报 DATA）；新增 `RISK_CONTENT_UPDATED` /
  `RISK_WINDOW_ADVANCED` 事件；
- Semantic digest 硬化：新增纯业务 `risk_semantic_digest`（NaN-safe）与
  `route_semantic_digest`（waypoints + metrics + layer/focus），mutation
  tests（wall-clock SAME / route 改 DIFFERENT / risk 改 DIFFERENT）；
- NavigationExecutionState v1：node-aligned same-vessel replan origin、
  显式 snap tolerance、completed-track 单调、无 teleport 校验、
  RouteSwitchGate 拒绝候选不采纳；
- 受控 objective 级并行 `replay/parallel.py`：单次 C request 三目标
  ProcessPoolExecutor，tick/layer/B 严格串行，进程内 patch 退出恢复；
  benchmark 1/2/3 worker = 157.2s / 100.9s / 80.5s（结果逐位一致）；
- `scripts/v3_contract_experiment.py`：真实 77h 窗口复现旧 main_corridor
  cap 失败并证明 72h 候选 cap 四层全 PASS（integrity PASS）；
- 3h same-vessel v3 smoke：4 snapshots、validation PASS、RSS≈824MB；
  12h 权威回放（v3 + 3 workers）：13 snapshots、2071.4s、plan_revision
  1→6、replan 全 `time`、completed-track 单调、validation 全 PASS；
- determinism：独立输出根复跑 13/13 snapshot digest + manifest digest
  一致（replay_id 与窗口 commit id 已从 semantic digest 排除）；
- 测试：replay unit 31 项通过，ruff 全绿。

## Unreleased - 2026-08-18（Causal Replay Engine MVP，Strategy B）

- 新增 replay 包：SimulationSnapshot/ReplayManifest 模型、两级 digest
  （visible / B-relevant）、event-driven runner（tick → A visibility →
  B reuse/recompute → suffix window → C policy → snapshot）、
  C 语义路线完整性审计、机器 validation 与 `replay_inspect.py` CLI；
- 新增 `causal_replay_preflight.py` 操作级 horizon 预检与
  `causal_replay_mvp.py` 运行 CLI（checkpoint/heartbeat/原子发布/resume）；
- 真实 Scenario B 因果回放：12h（13 snapshots/31s）、24h（25/91s）、
  44h（45/317s）PASS（engine + validation）；determinism 13/13 snapshot
  digest 与 manifest digest 一致（B generated_at 改用 knowledge 时刻）；
- C 四层：因果风险窗 44h < 航线 ETA ~48h → 真实 PlanningHorizonExceeded
  （v3 + v2 probe），记录 PLANNING-HORIZON ARCHITECTURE BLOCKER；
  未修改 frozen retrospective 路径；测试：replay unit 21 + 既有 25。

## Unreleased - 2026-08-18 第二轮（Causal Planning Horizon Resolution）

- 三窗口解耦：`risk_forecast_end`（77h，08-18T15:00Z）独立于
  `replay_end`；规划场景 horizon 用 corridor 对齐值（96h），C 查询
  maximum_elapsed 限制在 risk forecast end；
- `--v2-only`：v3 four-layer 失败时 v2 complete-route fallback 并正式
  集成（replan input_revision 严格递增、REPLAN_TRIGGERED/ROUTE_CHANGED）；
- 真实 12h 集成回放：13 snapshots、plan_revision 1→13、B build 1 次 +
  reuse 12、route integrity 13×3 全 PASS、validation PASS；
- determinism：13/13 snapshot digest + manifest digest 一致，
  generated_at（墙钟）允许不同（语义 digest 排除 resource/route 身份）；
- 性能：12h≈36–38min、peak RSS≈824MB、C 规划占耗时 ≥95%；
- v3 four-layer = main_corridor contract-edge blocker（cap 无余量），
  本轮不改 C，记录为 TD-32。

## Unreleased - 2026-08-17（Route Geospatial Integrity）

- 编排器核心无改动；D 新增 Route Geospatial Integrity gate
  （`demo geo-integrity` + preflight 硬门），对冻结 v3 制品与 committed
  risk frames 做 waypoint/edge/角切/时间映射/投影一致性机器审计；
  Viewer 双投影 bug 在 D 侧修复，路线与格子共用同一地理投影。

## Unreleased - 2026-08-17（Demo Candidate 2）

- Demo live runner 无核心变更；D `demo serve` 新增
  `POST /api/live/start` / `GET /api/live/status`，可由 Viewer 页面按钮
  直接驱动 `demo_live_runner.py`（真实 worker + watchdog），
  实测 UI 触发 ≈56s、LIVE_COMPUTED、无重复进程（单飞守卫）。

## Unreleased - 2026-08-17（RC2 development）

- Demo Engineering：新增 `scripts/demo_live_worker.py`（真实小窗 replan，
  输出 `d.live-result.v1` / LIVE_COMPUTED）与 `scripts/demo_live_runner.py`
  （worker + watchdog 超时驱动，实测 57.6s ≤2min）。
- B 完成后释放 `build_request/envelope`，endpoint mapping 后释放
  `PreparedWindow`（`replace(intake, prepared_window=None)`），C 阶段不再
  驻留 A 大帧；mur 全链峰值 RSS 4.18GB → 2.81GB、Tromsø 144h 1.40 → 0.97GB，
  业务输出不变。
- 新增公平 objective 级并发 benchmark
  `scripts/bench_equal_work_objectives.py`（serial/parallel/prototype 三模式，
  同一风险窗相同工作量）：2-worker speedup ≈1.48×、合计峰值 <330MB、
  业务输出与串行一致；prototype = EXPERIMENTAL / 正式路径串行。
- `verified_build_snapshot` 深拷贝消除后，RC1/Scenario B 全链复跑验证
  business regression 不变；Scenario B 144h r2 与 r1 digest/业务完全一致。
- 新增 `coverage_preflight` 阶段：B full commit 后、endpoint mapping/C 前计算
  `planning-coverage-preflight.json`（schema v1），逐帧报告 total/hard/land/
  data_unavailable/navigable/unknown-navigable 与 missing_input_variable_counts；
  gate 语义 = 每帧 unknown_navigable_nodes==0，失败在规划前报
  `coverage_preflight_failed`；C 自身 RiskSamplingError fail-closed 保持不变。
- 新增 RC1 golden 回归脚本 `scripts/rc1_golden_regression.py`：校验 r6/r7 的
  initial/replanned layer-set 语义 digest 与 checksums。
- 修复 CLI `run` 消费 worker 结果：`run_with_timeout` 返回 dict，CLI 原按
  `FormalRunResult` 属性访问导致成功运行后报错；修复并新增 2 项 CLI 单测。
- `timeout_runner` 增加 `ORCH_DEBUG_TIMEOUT=1` 环境变量调试输出（逐事件/逐阶段
  elapsed），默认关闭，不影响正常行为。
- 集成 fixture 改用 RC1 实际 B 配置 `demo_unvalidated_smoke_grid_v4.json`
  （默认 0.75°×2.2° 网格与 corridor 2.2.0 终点允许区域无交点）；
  integration 阶段集合同步加入 coverage_preflight。
- coverage preflight 每帧新增可选 `ice_free_neutralized_nodes`（取自 B 帧
  `ice_free_neutralized_input_counts` attrs），旧文档无该字段仍可校验。
- 新增 Scenario B regression：`scripts/rc2_second_scenario_regression.py`
  （Tromsø 144h v3 + coverage + D + golden digest 校验）。
- 测试：非集成 18 passed（含 coverage preflight 与 CLI 单测）。

## Unreleased - 2026-08-15

## Unreleased - 2026-08-16（RC1）

- 新增真正可中断 per-stage timeout：`stage_worker.py`（隔离 worker 执行正式链）+
  `timeout_runner.py`（parent watchdog，按心跳逐阶段 wall-clock 超时，SIGTERM→宽限→
  SIGKILL，无孤儿，TIMEOUT 报告含 elapsed/timeout/last_progress）；CLI `run` 切换；
  3 项单测覆盖正常/超时/失败路径。
- `execute_formal_run` 增加可选 heartbeat 钩子（默认关闭，行为不变）。
- 完整 E2E 首次成功：mur/dikson v3 四层 + 6h 重规划（r6），r7 复现业务级一致。

- 创建项目内 Mamba Python 3.13.15 + uv 0.12.4 环境并生成 `uv.lock`；该环境修复不改变包版本。
- 新增阶段可观测性：`ExecutionSpec.per_stage_timeout_seconds`（默认 900 s）、七阶段
  开始/耗时/状态记录、单阶段超时抛 `stage_timeout`，失败或超时原子落盘
  `run-stage-report.json`（含已完成阶段与错误）；execution-spec v1 Schema 与示例同步更新。
- Ruff 与 11 项非集成测试通过（含 stage-report 单元测试）。完整 v2/v3 集成成功路径尚未
  在本机跑通：900 s 默认超时已在 `c_replanning` 触发并正确落盘失败报告（机制验证通过），
  放宽预算后在 `c_initial_planning` 阶段超 1 小时人工中断，仍需小窗或长会话补验。
- 确认 v3 四层 × 三目标为演示主线、v2 三目标为强制后备（2026-08-15）；Makefile 新增
  `smoke`（快速门禁）、`integration-v2`、`integration-v3` 分层目标，`check` 保持发布级。
- intake 从“硬编码主走廊 + 恰好 168 h”改为按 RunContext/Scenario 校验走廊与完整窗口；
  12 类画像与 fail-closed 规则不变。这是 tromso 冻结数据可经 orchestrator 运行的前提。

## 0.1.0 - 2026-08-13

- 建立独立 Python 3.13、Mamba + uv 工程。
- 新增严格、可复现的 execution spec。
- 新增外部 A `DatasetBundle v2 + RunContext v2` 接收门，拒绝旧 v1/9 类或不完整来源。
- 预留只通过公共 API 运行 A→B→C 及输出审计报告的应用边界。
