# Changelog

## Unreleased - 2026-08-14

- 创建项目内 Mamba Python 3.13.15 + uv 0.12.4 环境并生成 `uv.lock`；该环境修复不改变包版本。
- Ruff、8 个非集成测试、lock/sync/CLI 检查通过；两项集成测试运行 24 分 49 秒后因缺少
  阶段性能预算人工中断，完整门禁仍待优化后重跑。

## 0.1.0 - 2026-08-13

- 建立独立 Python 3.13、Mamba + uv 工程。
- 新增严格、可复现的 execution spec。
- 新增外部 A `DatasetBundle v2 + RunContext v2` 接收门，拒绝旧 v1/9 类或不完整来源。
- 预留只通过公共 API 运行 A→B→C 及输出审计报告的应用边界。
