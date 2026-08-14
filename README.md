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
- 2026-08-14 formal-shape 长运行只到 B full/suffix 和 C v2 初始三目标；重规划未形成
  最终 output，v3 未开始，且夹具不是实际下载数据。
- 标准 `make check` 因没有可发现的 Python 3.13 前缀失败，当前没有标准环境通过证据。
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

环境门禁修复后仍须分阶段运行 v2、重规划和 v3；不要直接重启无心跳、无阶段超时的完整
长用例。
