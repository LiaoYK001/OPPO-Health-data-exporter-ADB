# MuMu + ADB：OPPO 健康全细节导出流程

## 1. 为什么开启 Root 后会“闪退”

OPPO 健康 6.5.4 会主动检测 Root。检测到后显示风险提示，确认后结束应用进程；这不是 MuMu 崩溃，也不是健康数据损坏。req1 因此固定采用“两阶段采集”：

1. Root 开启：只复制数据库、WAL/SHM，并在必要时提取数据库密钥；
2. Root 关闭：正常打开 OPPO 健康，执行全部 UI 页面遍历。

不要反复点击风险提示，不要清除 OPPO 健康数据，也不要卸载/重装 APP。无需 Magisk、LSPosed 或 Root 隐藏模块。

## 2. 本轮数据安全状态

已经成功保存过一份 Root 原始快照，清单在：

`data/manifests/oppo-snapshot-5182eb1b-6f55-4832-bbe8-7730a8c9c4e4.json`

其中主健康库 `database.db` 使用 SQLCipher 加密。快照及其 WAL/SHM 已按 SHA-256 登记；关闭 Root 不会删除这份本地快照。当前可以直接从第 4 节继续。

## 3. 以后重新建立 Root 快照

先在 OPPO 健康内完成同步，然后关闭 APP。打开 MuMu“设置中心 → 其他设置 → Root 权限”，保存并重启模拟器。

```powershell
python .\ohealth_exporter.py doctor --phase source
python .\ohealth_exporter.py snapshot-source
```

硬性条件是 `adb shell su -c id` 返回 `uid=0`。`snapshot-source` 会先复制数据库和相邻 WAL/SHM，再检查副本，避免读取设备侧 SHM 时造成锁冲突。成功输出 manifest 后即可关闭 Root。

若主库仍报告 SQLCipher 加密，则在同一次短暂 Root 窗口安装可选客户端并截取 APP 自己传给数据库的密钥：

```powershell
python -m pip install -e ".[frida]"
python .\ohealth_exporter.py key-capture
```

命令会根据客户端版本从 Frida 官方 GitHub Release 下载匹配 MuMu ABI 的 Server，校验发布页提供的 SHA-256，临时推送到 `/data/local/tmp`，启动 OPPO 健康并截取 `AppDatabase` 初始化参数。随后由 APP 已加载的 SQLCipher 对数据库做临时明文导出，拉回本地、登记素材哈希并立即规范化；不修改源数据库。密钥只写入 `data/private/oppo-database.passphrase`，终端不会显示明文；结束时会删除设备侧明文临时文件、停止 Server 并移除 ADB 端口转发。

## 4. 关闭 Root，恢复 APP 正常运行

1. 完全关闭 MuMu；
2. 在 MuMu 设置中心关闭 Root 权限，保存；
3. 重新启动 MuMu；
4. 打开 OPPO 健康，确认不再出现 Root 风险提示；
5. 等待同步完成，切到底部“健康”页；
6. 不改分辨率、密度、字体、语言或深浅色模式。

然后执行：

```powershell
python .\ohealth_exporter.py doctor --phase ui
```

预期：ADB、1440×2560、640、APP 版本、Root 阶段状态、前台应用和磁盘空间均为 `[OK]`。如果“前台应用”失败，只需手动把 OPPO 健康切回前台再重跑。

## 5. 健康首页盘点

```powershell
python .\ohealth_exporter.py inventory
```

采集器自动识别活动、心率、睡眠、血氧、血压、体重和压力，并记录所有未知卡片。健康趋势、AI 健康助手、健康旅程、排行、勋章/成就、习惯打卡、提示、身心状态和底部独立“运动”页不进入 req1；指标详情内的目标、评分、区间、评价和说明仍会保存。

任何未知原始健康卡片都会写入 `failures`，不得静默跳过。

## 6. 页面试跑与全历史采集

先每项试跑一天：

```powershell
python .\ohealth_exporter.py collect --ui-only --metrics activity,heart_rate,sleep,spo2,blood_pressure,weight,stress --max-days 1
```

确认页面导航正确后全量运行：

```powershell
python .\ohealth_exporter.py collect --ui-only --all-history --resume
```

每个“指标 × 日期”会保存：

- 所有滚动视口的 PNG 和 UI XML；
- 页面全部非装饰文字及控件坐标；
- 拼接长图和图表裁剪；
- 页面、日期、APP 版本、显示配置和素材 SHA-256；
- `exact_ui`、`no_data` 或 `failed` 覆盖状态。

`Ctrl+C`、模拟器重启或 APP 被系统杀死后，重新执行同一命令即可从 checkpoint 续跑。历史完成日跳过；当天属于 `partial`，即使已有 checkpoint 也会重新采集并生成可追溯版本。

## 7. 精确数据边界

截图仅用于验真，不能根据柱高或折线位置反推数值并冒充精确值。图表的步数时间片、心率/血氧采样、睡眠阶段等必须来自解密后的原始数据库或 APP 数据服务。当前主库已确认加密，在密钥提取完成前，相应精细序列必须标记为阻断缺口，不能把 `visual_only` 当成语义完整。

Root 风险提示不会阻止已经完成的快照，但数据库密钥提取需要另一次短暂 Root 窗口。该操作只读取运行时返回值和建立临时导出，不修改 OPPO 健康源数据；完成后仍应立即关闭 Root。

## 8. 导出和验收

```powershell
python .\ohealth_exporter.py verify
python .\ohealth_exporter.py export --format both
```

输出包括 `daily_overview.csv`、各指标日表 CSV、`coverage.csv` 和 `health_daily.xlsx`。SQLite 继续保留日内点位、阶段区间、原始行版本和证据关系。

req1 最终验收要求：

- 目标指标覆盖率 100%，未知失败为 0；
- 每个指标日期都有成功、无数据或失败状态；
- 整数与 APP 完全一致，浮点按 APP 显示精度核对；
- 时长允许 APP 分钟舍入造成 1 分钟显示差异，但原始秒数保留；
- 所有非装饰图表具有精确序列；
- 所有素材 SHA-256 可复算；
- CSV/XLSX 可仅从 SQLite 重复生成。

## 9. 隐私与容量

`data/` 含健康数据、账号/设备原始字段、截图和数据库，已加入 `.gitignore`。不要将其上传到公开仓库。多年逐页证据可能占用 10–50GB；全量开始前预留空间，完成后制作两份加密备份。
