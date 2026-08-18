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
  影响。详见 `CAUSAL_REPLAY_MVP_20260818.md`。
- Causal Planning Horizon Resolution（2026-08-18 第二轮）：replay /
  risk forecast / planning 三窗口解耦（77h causal forecast）；
  `--v2-only` 提供 v2 complete-route 真实规划并集成 12h 回放
  （plan_revision 1→13、route integrity PASS）；v3 four-layer =
  main_corridor contract-edge blocker（待 C 合同 proposal）。

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
