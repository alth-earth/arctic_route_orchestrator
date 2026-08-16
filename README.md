> **文档治理声明**
>
> - 本文件角色：当前 A–B–C 根级编排器短入口。
> - 改造时间：2026-08-14（Asia/Shanghai）。
> - 原文归档：[README.archive-20260814-pre-governance.md](README.archive-20260814-pre-governance.md)。
> - 改造原因：保留快速启动入口，同时把状态、阻塞、事故证据和继续开发顺序集中到统一 handoff。

# A–B–C 根级运行器

`arctic-route-orchestrator` 只通过共享契约和 A、B、C 公共 API 接收制品、运行风险/规划链并
输出审计报告。包元数据当前为 `0.1.0`；不依据 HEAD 提交信息擅自改成 0.1.1。

## 当前状态

- 严格 A 制品接收门和跨包运行骨架已实现。
- 2026-08-15 新增阶段可观测性：七阶段开始/耗时/状态记录、单阶段超时（默认 900 s）与
  失败报告 `run-stage-report.json`；`per_stage_timeout_seconds` 为 execution-spec v1 新字段。
- 2026-08-14 formal-shape 长运行只到 B full/suffix 和 C v2 初始三目标；重规划未形成
  最终 output，v3 未开始，且夹具不是实际下载数据（历史事故，详见复盘）。
- 编排器自己的 Mamba+uv 标准环境已创建：Python 3.13.15、uv 0.12.4，且已生成并纳入
  `uv.lock`；A、B、C 的既有标准环境不受此次补齐影响。静态检查、8 个非集成测试、lock/sync
  检查和 CLI 帮助均通过。完整 `make check` 的 v2/v3 集成成功路径仍未收口：默认 900 s 超时
  已在 `c_replanning` 正确触发并落盘失败报告，但放宽预算后仍在 `c_initial_planning` 阶段
  超 1 小时，需小窗或长会话补验。
- 成功报告仍为 `demo_unvalidated`，并明确 `navigation_use=prohibited`。

## 接手顺序

1. [编排器项目交接](arctic_route_orchestrator_handoff.md)
2. [长集成运行事故复盘](docs/INCIDENT_2026-08-14_LONG_INTEGRATION_RUN.md)
3. [系统整体架构](../ARCTIC_ROUTE_SYSTEM.md)
4. [当前十日冲刺](../ABC_10_DAY_SPRINT.md)
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
