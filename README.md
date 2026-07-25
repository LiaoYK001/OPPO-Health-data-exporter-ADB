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

#### 采集中断与 checkpoint

采集器在每个日期完整处理并写入 SQLite 后更新 checkpoint。普通手工采集被 `Ctrl+C`、APP 退出或 ADB 暂时断开而中断时，重新执行上面的最后一条 `collect --ui-only --all-history --resume` 命令即可继续：

- checkpoint 之前已经完成的历史日期会跳过，不会从头重新抓取；
- 中断时尚未完整提交的日期可能重新采集一次，以保证页面证据完整；
- 当天数据属于 `partial`，再次同步时会生成一个新版本，旧版本和旧素材不会被删除或覆盖。

续跑时不再从最新日期逐日翻到 checkpoint。采集器会打开详情页日期右侧的下拉箭头，进入日历，以月为单位滚动到 checkpoint；每日活动优先点击日历暴露的精确日期节点，睡眠等 Canvas 日历则按真实月份网格计算日期坐标，并统一以详情页显示的完整日期做严格确认。即使目标月份标题已经滑出屏幕，也会根据下一个月份标题识别仍然可见的上月日期。只有日历控件不可识别或确认失败时，才退回原来的逐日快速翻页方式。因此无需人工预先跳转日期。

#### 电脑计划重启：先安全暂停

电脑需要正常重启、关机或安装系统更新时，不要直接在任务管理器中结束 Python，也不要先关闭 MuMu。先在项目目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\pause_ui_collection.ps1 -Workspace "C:\Users\czl1\Desktop\Project\OPPO-Health-data-exporter-ADB"
```

暂停脚本会依次执行以下操作：

1. 创建 `data\STOP_UI_COLLECTION`，防止监督脚本重新启动采集器；
2. 先停止后续指标监督器，再停止当前页面采集器；
3. 将当前采集批次标记为 `interrupted`，保留已经提交的 SQLite checkpoint；
4. 在终端打印各指标已经到达的日期。

只有看到下面两行成功提示后，才关闭 MuMu 或重启电脑：

```text
采集已安全暂停，checkpoint 已保存在 data\health.sqlite3。
现在可以重启电脑。
```

如果暂停脚本报错，不要立即重启；保留终端输出并排查错误。脚本在成功完成前不会把已有采集数据删除。

#### 开机后恢复采集

按以下顺序操作：

1. 启动 MuMu，等待 Android 系统和 ADB 完全就绪。
2. 确认 MuMu 的 Root 已关闭。如果 Root 仍然开启，先关闭 Root，再打开 OPPO 健康，否则 APP 可能显示风险提示后退出。
3. 不要改变 1440×2560 分辨率、640 密度、中文语言和深色模式。
4. 打开 OPPO 健康，进入“健康”首页，等待卡片和当天数据加载完成。
5. 在项目目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\resume_ui_collection.ps1 -Workspace "C:\Users\czl1\Desktop\Project\OPPO-Health-data-exporter-ADB"
```

恢复脚本会先检查 ADB 连接、显示配置、APP 版本、前台 APP、剩余磁盘空间及 Root 是否关闭。检查全部通过后，它才会删除 `STOP_UI_COLLECTION` 并修复重启前遗留的运行状态。若心率 checkpoint 已等于心率最早来源日期，脚本会跳过重复心率采集，直接衔接活动、睡眠、血氧、血压、体重和压力；否则先从心率 checkpoint 继续。

看到“页面采集已从 SQLite checkpoint 继续”以及心率采集和后续指标调度的 PID 后，即表示恢复成功。之后保持 MuMu 和电脑运行，不要操作模拟器，也不要开启 Root。运行日志保存在 `data\logs\`。

如果环境检查未通过，恢复脚本不会启动采集，也不会删除停止标志。根据终端提示修正 MuMu、Root 或 OPPO 健康状态后，重复执行恢复命令即可。

#### 突然断电或来不及暂停

如果电脑已经死机、断电或强制重启，无法提前执行暂停脚本，开机后仍按“开机后恢复采集”的步骤操作。恢复脚本会把没有对应存活进程的旧 `running` 批次改为 `interrupted`，再从最后一次成功提交的 checkpoint 继续。最坏情况下只会重新采集最后一个未完整提交的日期；已完成历史日期、原始证据和旧版本不会被覆盖。

### 本地可视化监控与调试日志

监控页只读取本机 SQLite、素材、运行进程和 `data\logs\`，不会启动、停止或操作采集器，也不会把健康数据发送到外网。在项目目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_dashboard.ps1 -Workspace "C:\Users\czl1\Desktop\Project\OPPO-Health-data-exporter-ADB"
```

浏览器会打开 `http://127.0.0.1:8765/`。页面可选择 1 秒、2 秒或 5 秒刷新，展示 ADB/Root/APP/采集进程状态、各指标 checkpoint、批次记录、未解决失败、素材数量和容量、日历跳转证据，以及可选择的完整日志尾部。中文 Windows PowerShell 日志会自动识别 UTF-8、UTF-16 或 GB18030 编码。

“实时详细操作”单独读取 `data/logs/operation-events.jsonl`。采集器会为进入指标、读取日期、打开/滚动日历、点击日期、逐屏滚动、保存 PNG/XML、进入和退出子页面、提交页面/checkpoint、回退及失败分别写入结构化事件。每条事件包含本地毫秒时间戳、`DEBUG/INFO/WARNING/ERROR` 级别、run ID、指标、日期、当前页面/Activity、动作、结果和附加上下文。页面支持级别筛选、100/300/1000 条尾部范围和自动滚到底部；选择 1 秒即为最快实时刷新。

也可直接运行：

```powershell
python .\ohealth_exporter.py dashboard --open
```

监控服务默认只监听 `127.0.0.1`。因为页面包含个人健康采集状态和日志，不要改成公网监听地址，也不要通过公网隧道转发。

### 校验和表格

```powershell
python .\ohealth_exporter.py reextract-source
python .\ohealth_exporter.py repair-normalized
python .\ohealth_exporter.py verify
python .\ohealth_exporter.py export --format both
```

`reextract-source` 会从最近一次不可变的明文 OPPO 数据库快照重新执行表映射和规范化，不连接 ADB，也不修改模拟器中的 APP 数据。新增或修正旧版 vendor 表映射后可运行此命令；已有原始版本不会重复写入。

CSV 和多 Sheet XLSX 位于 `data/exports/<时间>/`。表格只保留日级汇总；日内采样点、睡眠阶段区间、原始 vendor 字段和页面证据保留在 SQLite/素材库中。

## 跨厂商适配边界

`VendorAdapter` 强制实现 `discover / snapshot / extract / normalize / capture / verify` 六个阶段。OPPO 当前使用同一接口连接 Root 快照、规范化、ADB 页面状态机和验真器；以后接入小米或华为时新增适配器即可。`sources` 通过 `vendor` 区分来源，指标、采样点、区间、日汇总、覆盖状态和素材关系等核心表没有 OPPO 专属必填字段。按 `source_id` 导出时，日表和 coverage 均会严格过滤该来源。

连接设备时会把 Android 版本、型号、ABI 等非账号元数据写入 `devices`，Android ID、ADB 序列号和构建指纹只以项目私有盐的 HMAC 保存，不落明文。严格验真还会反向扫描 `data/artifacts/sha256`：除校验数据库登记素材的大小与 SHA-256 外，未登记的孤儿文件和不符合内容寻址目录规则的文件也会阻止最终验收。

完整状态、故障处理和验收边界见 [MuMu 操作流程](document/MUMU_WORKFLOW.md)。

## 开发验证

```powershell
python -m pytest
python -m compileall -q health_exporter ohealth_exporter.py
```

健康数据、截图、UI XML、数据库快照和导出文件都属于敏感个人信息，不要上传到公开仓库、网盘或 issue。
