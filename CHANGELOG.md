# Changelog

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
