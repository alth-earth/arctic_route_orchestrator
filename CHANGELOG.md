# Changelog

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
