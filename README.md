# OPPO Health data exporter (MuMu + ADB)

这个项目用于从 MuMu 模拟器里的国内版欢太健康（`com.heytap.health`）保存个人可见健康数据。当前第一阶段已经实现环境检查、屏幕/UI 快照，以及心率详情页单日结构化探针。

## 立即开始

前提：MuMu 12 已运行，欢太健康已登录并完成同步，MuMu 的“ADB 本地调试”已打开。

```powershell
python .\ohealth_exporter.py doctor
```

保存当前页面现场（PNG、UI XML、可见文字）：

```powershell
python .\ohealth_exporter.py capture
```

在欢太健康首页点击“心率”卡片，进入“日”视图后运行：

```powershell
python .\ohealth_exporter.py probe-heart
```

输出会写入 `output\`。该目录包含个人健康数据，已加入 `.gitignore`，不要提交到公开仓库。

完整实施顺序、人工操作节点和验收标准见 [document/MUMU_WORKFLOW.md](document/MUMU_WORKFLOW.md)。
