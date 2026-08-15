> **文档治理声明**
>
> - 本文件角色：当前 A–B–C 根级编排器的人类与 AI 统一交接入口。
> - 改造时间：2026-08-15（Asia/Shanghai）。
> - 原文件去向：[arctic_route_orchestrator_handoff_归档_20260815.md](arctic_route_orchestrator_handoff_归档_20260815.md)。
> - 改造原因：同步挑战杯稳定演示、工程优先验收、双运行模式和项目负责人决策权。

# A–B–C 编排器项目交接

## 1. 目标与边界

编排器只经公共合同/API 接收 A 冻结制品，触发 B 风险窗口、C 规划/重规划并输出报告、JSON、
GeoJSON 和校验清单。D 未来只读这些完整制品。

编排器不下载数据、不选择科学参数、不访问包内私有数据库，也不把 fixture 或
`demo_unvalidated` 写成真实导航结论。

## 2. 挑战杯运行模式

| 模式 | 编排器职责 |
|---|---|
| 历史回放/验证 | 执行 issue/as-of/simulation 门禁，保存可见性和重规划证据 |
| 稳定演示 | 从本地冻结 A/B 制品启动，可无网重复演示并至少完成一次重规划 |

稳定演示是比赛默认路径；现场网络和数据最新性不作为完成条件。

## 3. 当前状态

| 维度 | 状态 |
|---|---|
| 包元数据 | 0.1.0；不在本轮擅自改版本 |
| intake/公共编排骨架 | 已实现 |
| 环境 | Python 3.13.15 + uv 0.12.4，`uv.lock` 已生成 |
| 非集成门禁 | Ruff、8 tests、lock/sync/CLI 通过 |
| 集成长运行 | 两项测试 24 分 49 秒后人工中断；需阶段预算和心跳 |
| 挑战杯完整链 | 待完成 D 展示和两次断网演示 |

已完成清单（源自：arctic_route_orchestrator_handoff_归档_20260815.md）：

| 功能 | 对应路径 |
|---|---|
| execution spec v1 模型、读取与语义校验 | `src/arctic_route_orchestrator/models.py`、`schemas/execution-spec-v1.schema.json` |
| 外部 A v2 制品严格接收门 | `src/arctic_route_orchestrator/intake.py` |
| A→B→C 公共 API 编排 | `src/arctic_route_orchestrator/service.py` |
| v2 与 v3 规划合同选择 | `src/arctic_route_orchestrator/models.py`、`service.py` |
| B full/suffix committed window 与 +6 h 重规划 | `src/arctic_route_orchestrator/service.py` |
| 原子输出、路由目录和 checksums | `src/arctic_route_orchestrator/output.py` |
| run report v1 | `schemas/run-report-v1.schema.json`、`service.py` |
| intake/run CLI | `src/arctic_route_orchestrator/cli.py` |
| 工程及 formal-shape fixture 测试 | `tests/` |
| 长运行证据与下一次手册 | `docs/INCIDENT_2026-08-14_LONG_INTEGRATION_RUN.md` |

当前 intake 固定 exact 168 h 是主走廊 MVP 验收门，不是共享系统所有走廊的永久时域规则。

## 4. 当前主线

```text
local frozen A bundle + RunContext
          ↓ intake
B full risk commit
          ↓
C v2 three routes
          ↓ simulation +6 h / risk update
B suffix commit → C replan
          ↓
atomic output/report/checksums → D
```

v2 三目标 + 重规划是比赛主线；v3 四层可在性能允许时作为增强，不能留下部分整组。

## 5. 未完成与待办

### P0

- 为 A、B、C、D 各阶段记录开始、结束、耗时、输入输出摘要和错误；
- 设置安全超时、取消点和失败时的部分报告；
- 用冻结演示场景完成至少两次断网运行；
- 确保旧 generation/request/revision 结果不会进入 D。

### P1

- 分段测量 C RiskSampler/A*，避免无心跳长运行；
- 有余量时单独验证 v3；
- 后续再考虑第二走廊通用 intake。

科学校准、CNN P2、新算法和实验 B 不属于当前编排器任务。

补充待办（源自：arctic_route_orchestrator_handoff_归档_20260815.md）：

- 在受控性能预算内分别完成 C v2 初始、+6 h 重规划及最终 output，再单独完成 v3；禁止把
  v2/v3 全量串在一个无阶段超时的长用例中；
- 保存失败/超时时的阶段、目标、展开量、采样次数、RSS 和已形成制品，避免只有成功时才有
  报告；
- 为 A、B build/commit、C 每目标规划、replan 和 v3 每层增加开始/结束/心跳与明确超时；
- 优化或推动 C 优化 RiskSampler 时间索引、帧数组缓存、重复 canonical snapshot 和多目标
  预处理复用；算法语义变化需由 C 单独评审；
- 包版本维持 0.1.0 还是补齐代码、CHANGELOG 和发布标签为 0.1.1，由项目负责人裁决。

## 6. 工程验收

```bash
cd /root/my_project/arctic_route_orchestrator
make lint
make test
make check
```

完整 `make check` 当前仍受集成长运行性能影响。挑战杯验收需要分阶段稳定完成，而不是无限等待
一个无遥测的全量测试。

## 7. 风险

- 当前 intake 主要针对主走廊 168 h；
- 长运行缺阶段持久报告和明确性能预算；
- D 尚未形成消费者；
- fixture 不等于比赛冻结数据；
- 所有结果仍是演示用途，禁止真实导航。

补充（源自：arctic_route_orchestrator_handoff_归档_20260815.md）：

- C 时间扩展 A* 与 RiskSampler 是已确认主要瓶颈；v2/v3 初始和重规划组合理论上串行 30 次
  搜索；
- run report 目前只在整个运行成功后形成；失败阶段缺少持久遥测；
- 快速粗网格只有 smoke 价值，没有路线保真或科学分辨率证据；
- HEAD 信息 `v0.1.1` 不等于包已发布 0.1.1，不得擅改版本或 CHANGELOG；
- bathymetry 和法律图层当前不是正式 hard constraints；输出不得用于导航。

## 7.1 数据、配置与模型位置（源自：arctic_route_orchestrator_handoff_归档_20260815.md）

编排器不拥有正式环境数据或模型权重；运行时通过参数引用：

| 输入/输出 | 位置或参数 |
|---|---|
| execution spec 示例 | `examples/murmansk-v3.execution-spec.json` |
| A bundle | `--bundle`，来自工作包 A 的外部 JSON 制品 |
| RunContext | `--run-context`，来自共享契约绑定的外部 JSON 制品 |
| A data root | `--a-data-root`，只供 A 公共 exact resolver 使用 |
| B 配置 | `--b-config`，正式配置在 `../work_package_b/configs/models/` |
| C 配置根 | `--c-config-root`，当前在 `../work_package_c/configs/` |
| 共享配置根 | `--contracts-config-root`，当前在 `../arctic_route_contracts/configs/` |
| B risk store | `--risk-store-root`，运行目录，不进 Git |
| 路线、报告和 checksums | `--output-dir`，运行目录，不进 Git |

CLI 操作示例（源自：arctic_route_orchestrator_handoff_归档_20260815.md）：

```bash
arctic-route-orchestrator intake \
  --bundle /path/to/dataset-bundle-v2.json \
  --run-context /path/to/run-context-v2.json \
  --a-data-root /path/to/a-data \
  --generation-id 0

arctic-route-orchestrator run \
  --execution-spec examples/murmansk-v3.execution-spec.json \
  --bundle /path/to/dataset-bundle-v2.json \
  --run-context /path/to/run-context-v2.json \
  --a-data-root /path/to/a-data \
  --b-config ../work_package_b/configs/models/demo_unvalidated_v2.json \
  --c-config-root ../work_package_c/configs \
  --contracts-config-root ../arctic_route_contracts/configs \
  --risk-store-root /path/to/run/risk-store \
  --output-dir /path/to/run/output
```

实际执行前必须按事故手册拆分 v2/v3、设置阶段超时和进度输出；上例只展示 CLI 字段，不是
授权直接启动完整长运行。最终验收至少确认：intake 全门禁、B full/suffix commits、C
initial/replan、正确路线数量、同一身份/digest、原子 output、checksums、失败报告、
耗时/RSS，以及 `demo_unvalidated/navigation prohibited` 警示。

## 8. 相关入口

- [README](README.md)
- [长运行复盘](docs/INCIDENT_2026-08-14_LONG_INTEGRATION_RUN.md)
- [系统权威](../ARCTIC_ROUTE_SYSTEM.md)
- [十日计划](../ABC_10_DAY_SPRINT.md)

项目负责人对 A/B/C 决策拥有最终权；Git 提交与同步在本会话结束后由用户手动执行。
