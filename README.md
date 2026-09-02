---
Overall Status: ACTIVE
Content Status:
  - COMPLETED
  - PLANNED
Document Role: CANONICAL
Applicability: CURRENT
Scope: orchestrator package README
Canonical For: orchestrator role and ownership
Branch: research-validation-system
Last Verified: 2026-09-02
Related Canonical Docs: ../arctic_route_governance/current/architecture/ARCTIC_ROUTE_SYSTEM.md
---

> **路径约定（2026-08-24）**：本文件中 `${ARCTIC_ROUTE_ROOT}` 为工作区根占位符，
> 指向包含各工作包目录（`arctic_route_contracts/`、`work_package_a/` 等）的公共根。
> 解析优先级：环境变量 > 当前所在目录 > `$HOME`。完整定义见
> `arctic_route_governance/README.md` 的"路径约定"章节。

# A-B-C-D Root Coordinator

## Winter 动态重规划与 RC2 三核并行（2026-09-01 22:40 +08:00）

默认 Winter Viewer 已恢复为 2026-08-31 使用的原始冻结身份，并消费同一身份的
`retrospective_dynamic_replay` 制品，而不是误选后续 holdout 或人工补写事件：回放保留原始
`issue_time`，明确标记为事后动态投影，不冒充当时可获得的 causal replay。C 正式
`cd.four-layer-route-plan-set.v3` 在初始规划和重规划中均使用 RC2 的三 worker
`ProcessPool`，只并行同一请求内的 fastest / low_risk / recommended 三个目标；tick、层级、
B 构建和 adoption gate 仍严格串行。manifest/report 会记录请求 worker 数、最大同时并发、
提交任务数和 worker PID provenance。

Viewer 传输保留每个真实 revision 的不可变四层 plan-set 资源（每版 4 层、12 条路线）以及
`REPLAN_DECIDED`、`REPLAN_ADOPTED`、`ROUTE_CHANGED` 事件，因此“待采用 / 已替代 / 当前路段”
由 revision 状态和正式 `motion_samples` 驱动。B-spline 仍是 D 的受约束绘制层，不能改变
waypoint、ETA、risk、adoption 或安全门禁。

当前到达态回放为 `winter-original-frozen-dynamic-v1`：25 个 snapshot、9 个不可变
revision、119 个真实事件，其中 `REPLAN_DECIDED/REPLAN_ADOPTED/ROUTE_CHANGED` 各 8 次；
最终状态为 `ARRIVED`，无 pending revision。原始身份为
`tromso_isfjorden_february_2026_research_v1`、DatasetBundle
`a-bundle-a2146dd0adbaa7db77a6beb7`、RiskWindow
`risk-window-sha256-b5bed6bb48893e32620710e8c765dc60ec37a2fc384f0c49014b92f0a1c056b2`。
C 运行报告记录 `requested/effective/max=3/3/3`、36 次规划调用和 108 个目标任务。默认 D
assembly 为 `winter-viewer-sha256-a375b431ed7c431487300988a7dc6c298cbecaf3a8e77ec4bc1371cf6be894e7`，
共 8,641 个分钟采样，逐 revision 传输 9 个候选集合和 9 个正式 motion set；R1–R4 为
`CURVE`，R5–R7 因 `integrated_risk_increased`、R8–R9 因几何条件诚实回退 raw。
最终工作树已通过 Orchestrator `145 passed`，并重新执行 formal integration：
`2 passed, 1 warning`、退出码 0、用时 1009.42 秒。告警仅为 xarray 探测不到可选
ecCodes/cfgrib plugin。全仓 Ruff 仍被无关既有脚本格式问题阻挡，所改文件的定向 Ruff 已通过。

## Winter Combined Viewer 组装（2026-08-23 20:14 +08:00）

> 历史基线；当前默认 Winter 路径已显式使用真实 `retrospective_dynamic_replay`。
> 当前原始冻结身份没有可诚实附加的 B explanation sidecar；strict causal 与 explanation
> 都必须使用独立、同身份的生产证据。

`scripts/replay_viewer_export.py` 现在支持 Winter research assembly：它同时校验
`DatasetBundle.v2`、`RunContext.v2`、committed `RiskWindow`、
`cd.four-layer-route-plan-set.v3`、`presentation.route-candidates.v1` 和 12-route
integrity evidence，然后输出与既有 D runtime 向后兼容的 `replay.viewer-bundle.v1`。

该模式不调用 A/B/C，也不把 Summer timeline 与 Winter artifact 混装。没有通过
`--winter-replay-manifest` 提供同身份 causal replay 时，航行仿真时间线只投影 C 已发布
full-voyage recommended route 的 waypoint ETA，并显式发布
`replanning_status=UNAVAILABLE_IDENTITY_BOUND_CAUSAL_REPLAY_REQUIRED`、空事件列表和
`source_replay=null`；不会伪造 `PLAN_COMPUTED` 或重规划事件。传入严格身份绑定的
`orchestrator.replay-manifest.v1` 后，才从真实快照/事件原样消费多个 revision、pending/
superseded 状态，并发布 `replanning_status=PUBLISHED_CAUSAL_REPLAY`。任何 identity、
geometry、metrics、RiskFrame membership 或 route-integrity 不一致均 fail closed。

D 仍是唯一 Viewer runtime owner；本仓库只拥有 assembly、preflight 和 JSON/PNG 输出。

## risk-explanation.v1 传输（2026-09-01）

`--risk-explanation-manifest` 只接受 B 发布的
`risk-explanation-manifest.v1`。Orchestrator 在传入 Viewer 前复核 manifest/status、sidecar
schema、相对 artifact 路径、artifact SHA-256，以及 `risk_window_id`、run/scenario/corridor/
vessel identity；失败时不嵌入 sidecar。Orchestrator 不计算 contributor、不修补缺测，D
继续可选消费并在缺失/错配时显示 `Explanation unavailable`。sidecar 的
`demo_unvalidated` / `research_unvalidated` 标识保持不变。

传输链本身已有其他 Winter 身份的工程验证，但当前原始冻结 RiskWindow 不附带 sidecar。
其精确 A source record 已在 2026-08-26 detided-retirement 清理中物理退役，无法从最终
RiskFrame 反推同次公式 component trace；因此默认 D 诚实显示 `Explanation unavailable`，
不能把其他 holdout 的 sidecar 错配过来，也不能伪造 contributor。该缺失不改变 RiskFrame、
路线或仿真。

## 研究曲线 sidecar 导出边界（历史兼容，2026-08-31）

`scripts/replay_viewer_export.py` 支持显式的 `--route-smoothing-sidecar PATH` 参数。该参数
只接受与 Winter authoritative recommended route 身份绑定、状态为 `ACCEPTED` 且带有效
digest 的 `c.research-route-smoothing-sidecar.v1`；通过后，sidecar 作为
`bundle.research_validation.route_smoothing` 和 `source_files.route_smoothing_sidecar` 的
研究构件一并导出。它只为历史实验和显式研究工具保留，默认 Viewer 不加载该 reader，
也不能作为正式 motion 失效时的生产 fallback。

这条路径只用于隔离研究回放：Orchestrator 不重算曲线风险、hard mask、coverage、正式 ETA
或船舶操纵性，也不修改 `cd.route-plan.v2/v3`、route metrics、replan adoption、formal
latest 或 frozen artifact。当前 C sidecar 是 `GEOMETRY_ONLY`，不构成生产资格或实际
航行控制证据。

## 正式工程 Route Motion 传输（2026-08-31）

Winter assembly 通过 `--route-motion-set PATH --require-route-motion` 接收 C 的
`cd.route-motion-set.v1`。Orchestrator 会重新校验四层 recommended plan ID、完整
waypoint/ETA/推荐速度 digest、RiskWindow、RunContext vessel、generation/revision 以及
initial/replan adoption 起终点。通过后，集合进入 bundle 顶层 `route_motion_sets`，其
`motion_set_id` 同时进入 assembly semantic identity；`combined_presentation` 用完整
`route_motion_set_ids` 和逐 revision layer/RiskWindow binding 授权多个重规划集合；研究 sidecar 仍留在
`research_validation`，两者不会互作 fallback。

默认 `work_package_d/viewer/` 目标会自动要求正式 motion；缺少参数会在导出前明确失败。
包含 plan/motion JSON 的正式输出可使用不可变目录发布器原子写入并由
`checksums.json` 绑定；Winter package 会同时保存四层 plan set 和
`route-motion-set.json` 传输副本（正式 motion 存在时），目录已存在时拒绝覆盖。Viewer export 也先写入
同级 staging 目录、manifest 最后落盘，再原子 rename。任一 motion 校验失败时不得注入
motion set；默认生产导出拒绝发布，已运行 bundle 在 D 运行时仍按 fail-closed 使用 raw
waypoint/timeline。本路径只代表工程仿真正式合同，
不声明实船校准、导航级 corridor 或 UKC。

## Research Validation role（2026-08-21 23:18）

The Orchestrator is the Pipeline / Artifact / Presentation Adapter. It owns
replay/navigation coordination, validation and export, but not the D Viewer runtime.
既有 Summer frozen package 的 candidate presentation 仍为 `NOT_PUBLISHED`；Winter
combined package 则投影已验证的真实 C artifacts，不排名或创造路线。

## Winter C validation 与候选投影（2026-08-23 10:20 +08:00）

`scripts/run_winter_c_validation.py` 可对既有 committed `bc.risk-frame.v2`
窗口执行正式 C v3 验证，并输出 `cd.four-layer-route-plan-set.v3`、GeoJSON、
integrity evidence 与 `presentation.route-candidates.v1` sidecar。该脚本不调用 A/B。

`replay_viewer_export.py --route-candidates PATH` 只接受严格验证且与 replay scenario
一致的 12-route sidecar。未传入时仍发布现有 `NOT_PUBLISHED` 空包。候选层、目标、几何、
ETA、risk metrics 和 selected identity 均从 C 原样投影；Orchestrator 不重新计算或排名。

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
authoritative cells with no interpolation, route drawing uses display-only
geometry with an authoritative-polyline fallback, and the Viewer may derive
presentation vessel position/heading from that curve using the timeline's raw
ETA and active-revision anchors. This export metadata does not alter
`bc.risk-frame.v2`, route waypoints, ETA, adoption, authoritative vessel-motion,
or causal replay semantics.

## Route display smoothing（2026-08-30 23:38 +08:00）

`VIEWER_PRESENTATION.route_rendering` now declares that the D Viewer may use
constrained local cubic B-spline paint geometry for planned route lines, with
authoritative-polyline linear densification as fallback. The metadata remains
presentation-only: it does not alter route waypoints, ETA, route metrics,
adoption, risk, or authoritative vessel-motion semantics; D owns the browser
implementation. D may also use the accepted display curve for Viewer-only
simulated position, heading, trail, and completed-track painting, anchored to
the raw ETA timeline and never written back to the bundle.

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
cd ${ARCTIC_ROUTE_ROOT}/arctic_route_orchestrator
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
