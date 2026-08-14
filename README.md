# A–B–C 根级运行器

`arctic-route-orchestrator` 只调用共享契约以及 A、B、C 的公共 API，把一个经过 A 精确恢复的
`DatasetBundle v2 + RunContext v2` 转为 B committed risk window 和 C RoutePlan。它不下载
数据、不读取 A 的 SQLite/私有目录，也不实现风险或规划算法。

当前仓库已包含外部 A 制品接收和跨包运行骨架，但完整集成尚未通过：2026-08-14 的
formal-shape 长运行完成到 B full/suffix commit 和 C v2 初始三目标，随后重规划没有形成最终
output，v3 未开始。原因和下一次分阶段运行规则见
[事故复盘](docs/INCIDENT_2026-08-14_LONG_INTEGRATION_RUN.md)。现存
`work_package_a/data/output/bundles/` 中的旧 v1/9 类文件仍会明确拒绝，不能冒充真实联调。

```bash
make env-create
make sync
make check

arctic-route-orchestrator intake \
  --bundle /path/to/dataset-bundle-v2.json \
  --run-context /path/to/run-context-v2.json \
  --a-data-root /path/to/a-data \
  --generation-id 0
```

正式接收门固定要求 Murmansk–Dikson、12 类必需层、168 小时完整 coverage、formal provenance
以及跨进程 exact resolver 成功。下载数据、凭据、缓存和运行输出均不得提交。
