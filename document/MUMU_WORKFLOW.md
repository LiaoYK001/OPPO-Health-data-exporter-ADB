# MuMu + ADB：OPPO 健康全细节导出流程

当前已在 MuMu Android 15（SDK 35）、1440×2560、密度 640 上完成 ADB、首页盘点、日期后退和 checkpoint 续跑验收。采集器仍保留 MuMu Android 12 的 ADB 路径兼容。

## 1. 为什么开启 Root 后会“闪退”

OPPO 健康 6.5.4 会主动检测 Root。检测到后显示风险提示，确认后结束应用进程；这不是 MuMu 崩溃，也不是健康数据损坏。req1 因此固定采用“两阶段采集”：

1. Root 开启：只复制数据库、WAL/SHM，并在必要时提取数据库密钥；
2. Root 关闭：正常打开 OPPO 健康，执行全部 UI 页面遍历。

不要反复点击风险提示，不要清除 OPPO 健康数据，也不要卸载/重装 APP。无需 Magisk、LSPosed 或 Root 隐藏模块。

## 2. 快照数据安全状态

每次成功的 Root 原始快照都会生成只保存在本机的清单：

`data/manifests/oppo-snapshot-<随机ID>.json`

主健康库 `database.db` 可能使用 SQLCipher 加密。快照及其 WAL/SHM 会按 SHA-256 登记；关闭 Root 不会删除本地快照。清单、数据库文件名、哈希和随机 ID 都不应出现在公开 issue 或 PR 中。

## 3. 以后重新建立 Root 快照

先在 Root 关闭时打开 OPPO 健康并完成同步。保持 APP 已打开，在 MuMu 设置中热开启 Root；不要在 Root 状态下重新启动 APP。随后执行：

```powershell
python .\ohealth_exporter.py doctor --phase source
python .\ohealth_exporter.py snapshot-source
```

硬性条件是 `adb shell su -c id` 返回 `uid=0`。`snapshot-source` 会先停止 APP 以冻结写入，再复制数据库和相邻 WAL/SHM，并检查副本；默认不会在 Root 状态下重新启动 APP。成功输出 manifest 后立即关闭 Root，再手动打开 APP。若确实需要恢复启动，可显式使用 `--restart-app`，但 OPPO 健康 6.5.4 通常会因 Root 检测而退出。

若主库仍报告 SQLCipher 加密，则在同一次短暂 Root 窗口安装可选客户端并截取 APP 自己传给数据库的密钥：

```powershell
python -m pip install -e ".[frida]"
python .\ohealth_exporter.py key-capture
```

命令会根据客户端版本从 Frida 官方 GitHub Release 下载匹配 MuMu ABI 的 Server，校验发布页提供的 SHA-256，临时推送到 `/data/local/tmp`，启动 OPPO 健康并截取 `AppDatabase` 初始化参数。随后由 APP 已加载的 SQLCipher 对数据库做临时明文导出，拉回本地、登记素材哈希并立即规范化；不修改源数据库。密钥只写入 `data/private/oppo-database.passphrase`，终端不会显示明文；结束时会删除设备侧明文临时文件、停止 Server 并移除 ADB 端口转发。

## 4. 关闭 Root，恢复 APP 正常运行

1. 在 MuMu 设置中心关闭 Root 权限并保存；
2. 确认 `adb shell su -c id` 不再返回 `uid=0`；
3. 手动打开 OPPO 健康，确认不再出现 Root 风险提示；
4. 如果热关闭未生效，再重启 MuMu，并保持 Root 关闭；
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

长时间采集应保持单一 UI 控制进程；不要同时启动两个 `collect`。如需把剩余指标排在正在运行的心率任务之后，可在隐藏窗口启动监督脚本：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\continue_after_heart.ps1 -HeartProcessId <心率采集PID>
```

监督脚本会在心率或后续多指标采集异常退出时从 SQLite checkpoint 自动续跑，默认每个阶段最多重试 3 次，并且不会重启 OPPO 健康。心率正常退出且 checkpoint 已到 SQLite 中该数据源的最早心率日期后，才会继续活动、睡眠、血氧、血压、体重和压力。采集结束后，校验与 CSV/XLSX 导出分开执行，因此即使完整校验报告仍列出待解决缺口，日级表格也会照常生成。运行日志位于 `data/logs/`。

如需因电脑重启而停止无人值守任务，不要手工结束 Python。请在项目目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\pause_ui_collection.ps1
```

脚本会先创建 `data\STOP_UI_COLLECTION`，停止监督器和采集器，把当前 run 标为 `interrupted`，并打印已提交的 SQLite checkpoint。看到“现在可以重启电脑”后再重启。

开机后按以下顺序恢复：

1. 启动 MuMu，确认 Root 关闭；不要改变 1440×2560/640、中文或深色模式。
2. 打开 OPPO 健康，停留在“健康”首页并等待页面加载完成。
3. 在项目目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\resume_ui_collection.ps1
```

恢复脚本会先运行 UI 阶段环境检查，检查通过后才删除停止标志。它会比较心率 checkpoint 与心率最早来源日期：若二者相等则跳过已经完成的心率，直接续跑活动、睡眠、血氧、血压、体重和压力；否则先续跑心率。若电脑突然断电、来不及执行暂停脚本，也直接按开机后的步骤恢复；遗留的 `running` 批次会被标为 `interrupted`，至多重抓最后一个未完整提交的日期。

恢复后无需人工逐日翻回中断日期，也无需先手工定位指标。采集器进入对应指标详情后，会点击日期右侧下拉箭头打开日历，按月份滚动到 checkpoint；每日活动直接使用日历提供的精确日期节点，睡眠等自绘日历使用真实月份网格计算坐标。目标月份标题滑出屏幕时，仍会根据下一个月份标题识别屏幕上属于上月的日期；点击后必须从详情页回读到完全相同的日期才算成功。只有日历打开、定位或日期确认失败时，才自动退回逐日快速翻页。日志中以 `[calendar]` 开头的记录会说明日历定位过程和最终确认结果。

每个“指标 × 日期”会保存：

- 所有滚动视口的 PNG 和 UI XML；
- 页面全部非装饰文字及控件坐标；
- 拼接长图和图表裁剪；
- 页面、日期、APP 版本、显示配置和素材 SHA-256；
- `exact_ui`、`no_data` 或 `failed` 覆盖状态。

`Ctrl+C`、模拟器重启或 APP 被系统杀死后，重新执行同一命令即可从 checkpoint 续跑。历史完成日跳过；当天属于 `partial`，即使已有 checkpoint 也会重新采集并生成可追溯版本。

使用 `--max-days N` 分批时，`N` 表示本批新采集或尝试的日期数；为了到达 checkpoint 而翻过的已完成日期不会消耗该额度。当日重抓也不会把已经推进到更早日期的历史 checkpoint 覆盖回今天。

## 7. 本地可视化监控与日历证据

如需随时查看进度、故障和详细日志，在项目目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_dashboard.ps1
```

打开 `http://127.0.0.1:8765/`。监控页是只读工具，不会删除 `STOP_UI_COLLECTION`，不会恢复采集器，也不会向 MuMu 发送点击。页面包括：

- ADB、Root、OPPO 健康前台页面及采集/监督进程；
- 各指标 checkpoint、日期范围、页面与素材数量；
- 最近运行批次、未解决失败和可切换的日志尾部；
- “打开日历—定位目标月份—单击目标日—详情页日期确认”的逐步截图、XML 哈希和验收结果。

页面默认每 5 秒刷新，也可在“实时详细操作”右上角改为 1 秒或 2 秒；1 秒是最快刷新频率。实时事件按时间顺序滚动，每条包含毫秒时间戳、`DEBUG/INFO/WARNING/ERROR` 级别、run ID、指标、日期、页面/Activity、实际动作、结果与调试上下文。可以筛选日志级别、选择显示最近 100/300/1000 条，并控制是否自动滚到底部。事件原文以 UTF-8 JSON Lines 保存在 `data/logs/operation-events.jsonl`，即使关闭网页也不会丢失。

原始进程日志阅读器兼容 UTF-8、带 BOM 的 UTF-16 和中文 Windows 常见的 GB18030；如果看到乱码，先刷新页面并确认服务使用的是当前代码。监控服务仅监听本机 `127.0.0.1`，不要对公网开放。

## 8. 精确数据边界

截图仅用于验真，不能根据柱高或折线位置反推数值并冒充精确值。图表的步数时间片、心率/血氧采样、睡眠阶段等必须来自解密后的原始数据库或 APP 数据服务。当前主库已确认加密，在密钥提取完成前，相应精细序列必须标记为阻断缺口，不能把 `visual_only` 当成语义完整。

Root 风险提示不会阻止已经完成的快照，但数据库密钥提取需要另一次短暂 Root 窗口。该操作只读取运行时返回值和建立临时导出，不修改 OPPO 健康源数据；完成后仍应立即关闭 Root。

## 9. 导出和验收

```powershell
python .\ohealth_exporter.py repair-normalized
python .\ohealth_exporter.py verify
python .\ohealth_exporter.py export --format both
```

`repair-normalized` 只修正规范层中已审计确认的映射错误，并在 `normalization_corrections` 保存修正前值、修正后值和原因；不可变原始数据库、原始行版本和页面证据不会被修改。重复运行是幂等的。

输出包括 `daily_overview.csv`、各指标日表 CSV、`coverage.csv` 和 `health_daily.xlsx`。SQLite 继续保留日内点位、阶段区间、原始行版本和证据关系。

req1 最终验收要求：

- 目标指标覆盖率 100%，未知失败为 0；
- 每个指标日期都有成功、无数据或失败状态；
- 整数与 APP 完全一致，浮点按 APP 显示精度核对；
- 时长允许 APP 分钟舍入造成 1 分钟显示差异，但原始秒数保留；
- 所有非装饰图表具有精确序列；
- 所有素材 SHA-256 可复算；
- CSV/XLSX 可仅从 SQLite 重复生成。

## 10. 隐私与容量

`data/` 含健康数据、账号/设备原始字段、截图和数据库，已加入 `.gitignore`。不要将其上传到公开仓库。多年逐页证据可能占用 10–50GB；全量开始前预留空间，完成后制作两份加密备份。公开提交前运行 `python .\scripts\public_release_check.py`，完整隐私说明见 [PRIVACY.md](../PRIVACY.md)。
