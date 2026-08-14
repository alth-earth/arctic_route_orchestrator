> **文档治理声明**
>
> - 本文件角色：当前 A–B–C 根级编排器的人类与 AI 统一交接入口。
> - 改造时间：2026-08-14（Asia/Shanghai）。
> - 原文归档：[README.archive-20260814-pre-governance.md](README.archive-20260814-pre-governance.md)。
> - 改造原因：补足目标边界、真实运行状态、性能事故、环境门禁、验收和继续开发顺序。

# A–B–C 根级编排器项目交接

## 1. 项目目标与边界

编排器负责把外部 A `DatasetBundle v2 + RunContext v2` 经过严格接收门，交给正式 B 形成
committed risk window，再调用 C 生成初始路线和同代次 `+6 h` 重规划结果，并保存带摘要、
环境信息、性能与文件校验和的运行报告。

编排器只调用公共 API：

- 不下载数据，不读取 A 私有 SQLite/raw/ready；
- 不实现风险模型或规划算法；
- 不替 B/C 选择科学参数；
- 不把 formal-shape fixture 或工程 `formal` 写成实源/科学完成；
- 不允许研究输出用于真实导航。

## 2. 当前状态

| 维度 | 状态 | 截止 2026-08-14 的准确含义 |
|---|---|---|
| 包元数据 | 已冻结/待确认 | `pyproject.toml` 与 CHANGELOG 为 0.1.0；HEAD 提交信息写 v0.1.1 |
| 严格 intake | 已完成 | 仅接收主走廊、v2、恰好 12 类、168 h、formal provenance 和 exact resolver |
| 跨包骨架 | 已完成 | 支持 v2/v3 execution spec、B full/suffix commit、C 初始/重规划和审计输出 |
| 标准工程门禁 | 阻塞 | `make check` 因没有可发现的 Python 3.13 前缀失败；未形成标准环境通过证据 |
| formal-shape v2 | 进行中 | 最后一次长运行完成初始三目标与 suffix，重规划无最终 output |
| formal-shape v3 | 待执行 | 最后一次长运行未开始 v3 |
| 真实实源链 | 待执行 | 依赖 A 的真实 12 类完整 bundle，目前不存在 |
| 科学校准 | 未完成 | 报告固定 `demo_unvalidated`、`navigation_use=prohibited` |

## 3. 已完成清单

| 功能 | 对应路径 |
|---|---|
| execution spec v1 模型、读取与语义校验 | `src/arctic_route_orchestrator/models.py`、`schemas/execution-spec-v1.schema.json` |
| 外部 A v2 制品严格接收门 | `src/arctic_route_orchestrator/intake.py` |
| A→B→C 公共 API 编排 | `src/arctic_route_orchestrator/service.py` |
| v2 与 v3 规划合同选择 | `src/arctic_route_orchestrator/models.py`、`src/arctic_route_orchestrator/service.py` |
| B full/suffix committed window 与 +6 h 重规划 | `src/arctic_route_orchestrator/service.py` |
| 原子输出、路由目录和 checksums | `src/arctic_route_orchestrator/output.py` |
| run report v1 | `schemas/run-report-v1.schema.json`、`src/arctic_route_orchestrator/service.py` |
| intake/run CLI | `src/arctic_route_orchestrator/cli.py` |
| 工程及 formal-shape fixture 测试 | `tests/` |
| 长运行证据与下一次手册 | `docs/INCIDENT_2026-08-14_LONG_INTEGRATION_RUN.md` |

当前 intake 固定 exact 168 h 是主走廊 MVP 验收门，不是共享系统所有走廊的永久时域规则。

## 4. 未完成与待办

### P0

- 依赖 A 产出主走廊真实 12 类、168 h 的 DatasetBundle v2、RunContext 和 doctor 证据。
- 在受控性能预算内分别完成 C v2 初始、+6 h 重规划及最终 output，再单独完成 v3；禁止把
  v2/v3 全量串在一个无阶段超时的长用例中。
- 保存失败/超时时的阶段、目标、展开量、采样次数、RSS 和已形成制品，避免只有成功时才有报告。
- 完成真实 A→B→C 身份、digest、169/163 帧、路线、重规划和输出 checksum 验收。

### P1

- 建立有效的项目内 Mamba Python 3.13 前缀，用 uv locked sync 后重新运行标准
  `make check`；不得用任意外部 `.venv` 结果替代。
- 为 A、B build/commit、C 每目标规划、replan 和 v3 每层增加开始/结束/心跳与明确超时。
- 优化或推动 C 优化 RiskSampler 时间索引、帧数组缓存、重复 canonical snapshot 和多目标
  预处理复用；算法语义变化需由 C 单独评审。
- 由负责人裁决包版本维持 0.1.0，还是补齐代码、CHANGELOG 和发布标签为 0.1.1。

### P2

- 决定是否把 intake 从主走廊基线扩展到 `tromso_to_isfjorden_outer` 动态 96 h 场景。
- 在真实网格性能证据完成后，再决定 D* Lite/LPA*/MPC 或并行多目标等架构升级。
- 对接未来 D 的 v3 原子整组消费者；D 不得读取未完成临时目录。

## 5. 技术架构与关键决策

```text
外部 A DatasetBundle v2 + RunContext v2 + data root
                         │
                    strict intake
                         │ exact public resolver
                         ▼
                PreparedWindow / RunContext
                         │
                         ▼
                  B full risk commit
                         │
                         ▼
                  C initial planning
                         │ +6 h
                         ▼
                 B suffix risk commit
                         │
                         ▼
                    C replanning
                         │
                         ▼
        atomic routes + run-report + checksums
```

关键决定：

1. A 只经公共 bundle/exact resolver；编排器不扫描 A 私有存储。
2. intake fail closed：旧 v1、错走廊、类型不等、覆盖/provenance 不完整、future 或身份不一致均拒绝。
3. execution spec 显式选择 `cd.route-plan.v2` 或 `cd.four-layer-route-plan-set.v3`。
4. B full/suffix 和 C initial/replan 保持同一 run、scenario、config、generation 身份。
5. 输出先写临时目录，再原子发布并生成内容校验和。
6. 当前报告的科学状态始终是 `demo_unvalidated`，不因工程运行成功而升级。

## 6. 已知问题、坑与风险

- formal-shape fixture 明确写有“not downloaded source data”，不能作为真实 A 证据。
- 最后长运行有效制品活动约 56 分钟；没有取回最终退出码，不能无证据判断为正常结束或某种特定失败。
- C 时间扩展 A* 与 RiskSampler 是已确认主要瓶颈；v2/v3 初始和重规划组合理论上串行 30 次搜索。
- run report 目前只在整个运行成功后形成；失败阶段缺少持久遥测。
- 快速粗网格只有 smoke 价值，没有路线保真或科学分辨率证据。
- 标准 `make check` 当前因缺 Python 3.13 前缀失败，故不能写“测试已通过”。
- HEAD 信息 `v0.1.1` 不等于包已发布 0.1.1；本轮不得擅改版本或 CHANGELOG。
- bathymetry 和法律图层当前不是正式 hard constraints；输出不得用于导航。

## 7. 数据、配置与模型位置

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

## 8. 操作与验收

### 标准环境

```bash
cd /root/my_project/arctic_route_orchestrator
make env-create
make sync
make check
```

2026-08-14 的准确状态是：标准 `make check` 因没有可发现的 Python 3.13 前缀失败。修复
环境后必须重新运行并记录实际退出码；不能沿用其他包或任意 `.venv` 的测试结果。

### 先做只读 intake

```bash
arctic-route-orchestrator intake \
  --bundle /path/to/dataset-bundle-v2.json \
  --run-context /path/to/run-context-v2.json \
  --a-data-root /path/to/a-data \
  --generation-id 0
```

### 再分阶段运行

```bash
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
授权直接启动完整长运行。

最终验收至少确认：intake 全门禁、B full/suffix commits、C initial/replan、正确路线数量、
同一身份/digest、原子 output、checksums、失败报告、耗时/RSS，以及
`demo_unvalidated/navigation prohibited` 警示。

## 9. 下一步计划与建议

1. 先修复并验证 Python 3.13 Mamba+uv 标准环境。
2. 在小窗、单目标、低节点 smoke 上补齐阶段遥测和超时。
3. 单独跑 v2 initial，再跑 v2 replan；通过后再单独跑 v3，复用已验哈希的 A/B 制品。
4. A 的真实 12 类制品形成后，重复同样分阶段流程并保存完整报告。
5. 性能证据稳定后再讨论第二走廊、算法替换和 D 集成。

## 10. 顶层与相关文档索引

- 当前短入口：[README.md](README.md)
- 治理前 README：[README.archive-20260814-pre-governance.md](README.archive-20260814-pre-governance.md)
- 事故复盘与运行手册：[INCIDENT_2026-08-14_LONG_INTEGRATION_RUN.md](docs/INCIDENT_2026-08-14_LONG_INTEGRATION_RUN.md)
- 版本记录：[CHANGELOG.md](CHANGELOG.md)
- 共享契约交接：[arctic_route_contracts_handoff.md](../arctic_route_contracts/arctic_route_contracts_handoff.md)
- A 交接：[work_package_a_handoff.md](../work_package_a/work_package_a_handoff.md)
- B 当前入口：[work_package_b/README.md](../work_package_b/README.md)
- C 当前入口：[work_package_c/README.md](../work_package_c/README.md)
- 系统权威：[ARCTIC_ROUTE_SYSTEM.md](../ARCTIC_ROUTE_SYSTEM.md)
- 当前冲刺：[ABC_10_DAY_SPRINT.md](../ABC_10_DAY_SPRINT.md)
- 梳理报告：[项目梳理报告.md](../项目梳理报告.md)
