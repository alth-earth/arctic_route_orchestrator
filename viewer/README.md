# Replay-driven Viewer MVP（2026-08-19）

从 authoritative replay artifact 显示真实 GEBCO 地图、Simulation Clock、
权威 route、completed track、pending/deferred adoption 以及 continuous moving
ship。Viewer 只消费后端 Presentation Adapter 生成的 `bundle.json`，不自行判断
planner/risk/adoption semantics，不制造 ship speed。

## 需要的制品

```text
authoritative replay artifact（Scenario B）：
  work_package_a/data/output/rc2-smoke/causal-replay-mvp/sb-viewer-baseline-12h-det/

本地 GEBCO 2026 land_sea_mask（已有，不下载）：
  work_package_a/data/raw/tromso_to_isfjorden_outer/land_sea_mask/**/*.nc
```

## 构建（生成 viewer 数据）

```bash
cd /root/my_project/arctic_route_orchestrator

# 1. 真实 GEBCO basemap（纯 Python PNG）
.venv/bin/python viewer/build_basemap.py \
  --data-root /root/my_project/work_package_a/data \
  --route-id tromso_to_isfjorden_outer

# 2. presentation preflight（L2 + transform + artifacts）
.venv/bin/python scripts/replay_viewer_preflight.py \
  /root/my_project/work_package_a/data/output/rc2-smoke/causal-replay-mvp/sb-viewer-baseline-12h-det/causal-replay-manifest.json \
  --data-root /root/my_project/work_package_a/data \
  --route-id tromso_to_isfjorden_outer \
  --output /root/my_project/work_package_a/data/output/rc2-smoke/replay-viewer-preflight.json

# 3. Viewer bundle（Presentation Adapter，1 分钟 cadence）
.venv/bin/python viewer/build_bundle.py \
  /root/my_project/work_package_a/data/output/rc2-smoke/causal-replay-mvp/sb-viewer-baseline-12h-det/causal-replay-manifest.json \
  --preflight /root/my_project/work_package_a/data/output/rc2-smoke/replay-viewer-preflight.json \
  --cadence-seconds 60
```

生成：`viewer/gebco_basemap.png`、`viewer/bundle.json`、
`viewer/basemap_metadata.json`。

## 运行（本地离线）

标准方式（127.0.0.1，提供静态文件 + `/api/state?t=...`）：

```bash
cd /root/my_project/arctic_route_orchestrator
.venv/bin/python scripts/replay_viewer_serve.py \
  --root viewer \
  --manifest /root/my_project/work_package_a/data/output/rc2-smoke/causal-replay-mvp/sb-viewer-baseline-12h-det/causal-replay-manifest.json \
  --port 8131
```

打开 `http://127.0.0.1:8131/`。

无 server 的单文件方式（把 bundle + basemap 内嵌进 HTML）：

```bash
.venv/bin/python viewer/embed.py --viewer-dir viewer
# 打开 viewer/index_self_contained.html
```

## 控件

`Play / Pause`、scrub、`1x / 2x / 4x / 8x`。演示时间压缩只改变
`simulation seconds / wall-clock second`，不改变业务船速。

## Debug view

右下角调试面板：

```text
simulation_time
vessel lon/lat
speed knots
edge progress
active plan revision
pending plan revision
pending plan status
decision time
effective adoption time
last event
L1 / L2 status
```

## 离线要求

无 CDN、无 remote JS/CSS/fonts/tiles；bundle 与 basemap 都来自本地 artifact。

## 验证制品（机器生成）

```text
output/playwright/replay-viewer-proof-1030.png
output/playwright/replay-viewer-proof-1330.png
work_package_a/data/output/rc2-smoke/replay-l2-preflight.json
work_package_a/data/output/rc2-smoke/replay-viewer-preflight.json
```
