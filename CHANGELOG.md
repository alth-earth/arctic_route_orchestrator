# Changelog

## 0.1.0 - 2026-08-13

- 建立独立 Python 3.13、Mamba + uv 工程。
- 新增严格、可复现的 execution spec。
- 新增外部 A `DatasetBundle v2 + RunContext v2` 接收门，拒绝旧 v1/9 类或不完整来源。
- 预留只通过公共 API 运行 A→B→C 及输出审计报告的应用边界。
