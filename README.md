# OPPO 健康全细节导出器（MuMu + ADB）

本项目采用两条采集通道：Root 阶段保存 OPPO 健康的数据库、WAL/SHM 和完整原始行；非 Root 阶段通过 ADB 遍历“健康”页及指标详情，保存逐屏 PNG、UI XML、长图、图表裁剪和页面文字。规范层统一写入 SQLite，并可重复导出日级 CSV/XLSX。

> OPPO 健康 6.5.4 会检测 Root 并主动退出。不要长期保持 Root，也不要安装 Root 隐藏模块。采集必须分成两个阶段。

## 当前环境

- MuMu：Android 15（SDK 35；同时兼容旧的 Android 12 ADB 路径）
- MuMu ADB：`127.0.0.1:7555`
- 包名：`com.heytap.health`
- 已验收版本：`6.5.4_48053ce_260715`
- 显示基准：`1440×2560`、密度 `640`、中文、深色模式
- 本地数据库：`data/health.sqlite3`
- 原始快照和页面素材：`data/`（已从 Git 排除）

## 两阶段操作

### A. Root 源快照

仅在需要新快照时使用短暂 Root 窗口。先在非 Root 状态打开 APP 并完成同步，保持 APP 已打开，再热开启 MuMu Root；不要在 Root 状态下重新启动 APP：

```powershell
python .\ohealth_exporter.py doctor --phase source
python .\ohealth_exporter.py snapshot-source
```

`snapshot-source` 会为一致性停止 APP，但默认不会在 Root 状态下重新启动。快照成功后立即关闭 Root，再手动打开 APP。只有已明确验证 Root 下可启动时，才使用显式的 `--restart-app`。

### B. 非 Root 页面采集

打开 OPPO 健康并停在“健康”首页：

```powershell
python .\ohealth_exporter.py doctor --phase ui
python .\ohealth_exporter.py inventory
python .\ohealth_exporter.py collect --ui-only --all-history --resume
```

中断后重复最后一条命令即可续跑；当天会重新采集并生成新版本。

### 校验和表格

```powershell
python .\ohealth_exporter.py repair-normalized
python .\ohealth_exporter.py verify
python .\ohealth_exporter.py export --format both
```

CSV 和多 Sheet XLSX 位于 `data/exports/<时间>/`。表格只保留日级汇总；日内采样点、睡眠阶段区间、原始 vendor 字段和页面证据保留在 SQLite/素材库中。

完整状态、故障处理和验收边界见 [MuMu 操作流程](document/MUMU_WORKFLOW.md)。

## 开发验证

```powershell
python -m pytest
python -m compileall -q health_exporter ohealth_exporter.py
```

健康数据、截图、UI XML、数据库快照和导出文件都属于敏感个人信息，不要上传到公开仓库、网盘或 issue。
