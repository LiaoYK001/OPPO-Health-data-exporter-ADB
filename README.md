# 多品牌健康数据中枢（OPPO / MuMu + ADB）

本项目是本地优先的多品牌健康与运动数据归一化平台。当前 OPPO 适配器采用两条采集通道：Root 阶段保存 OPPO 健康的数据库、WAL/SHM 和完整原始行；非 Root 阶段通过 ADB 遍历“健康”页及指标详情，保存逐屏 PNG、UI XML、长图、图表裁剪和页面文字。所有厂商数据写入同一套 SQLite 规范层，并可通过响应式 Web、CSV 和 XLSX 持续查看。未来接入小米健康或华为健康时，已有历史不需要搬迁或覆盖。

> OPPO 健康 6.5.4 会检测 Root 并主动退出。不要长期保持 Root，也不要安装 Root 隐藏模块。采集必须分成两个阶段。
>
> **隐私警告：** `data/`、`.diagnostics/`、`output/`、运行日志、截图和导出表格均可能包含健康信息或账号/设备标识。它们已被 Git 忽略，但提交前仍必须运行 `python .\scripts\public_release_check.py`。不要把真实数据附在 issue、PR 或公开网盘中。

## 已验收环境

- MuMu：Android 15（SDK 35；同时兼容旧的 Android 12 ADB 路径）
- MuMu ADB：`127.0.0.1:7555`
- 包名：`com.heytap.health`
- 已验收版本：`6.5.4_48053ce_260715`
- 显示基准：`1440×2560`、密度 `640`、中文、深色模式
- 本地数据库：`data/health.sqlite3`
- 原始快照和页面素材：`data/`（已从 Git 排除）

## 安装

项目目前以 Windows + MuMu 为主要验收环境。建议使用独立虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e .
```

只有数据库解密流程需要可选 Frida 依赖：`python -m pip install -e ".[frida]"`。开发和安全审计工具可用 `python -m pip install -e ".[dev]"` 安装。

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
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\pause_ui_collection.ps1
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
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\resume_ui_collection.ps1
```

恢复脚本会先检查 ADB 连接、显示配置、APP 版本、前台 APP、剩余磁盘空间及 Root 是否关闭。检查全部通过后，它才会删除 `STOP_UI_COLLECTION` 并修复重启前遗留的运行状态。若心率 checkpoint 已等于心率最早来源日期，脚本会跳过重复心率采集，直接衔接活动、睡眠、血氧、血压、体重和压力；否则先从心率 checkpoint 继续。

看到“页面采集已从 SQLite checkpoint 继续”以及心率采集和后续指标调度的 PID 后，即表示恢复成功。之后保持 MuMu 和电脑运行，不要操作模拟器，也不要开启 Root。运行日志保存在 `data\logs\`。

如果环境检查未通过，恢复脚本不会启动采集，也不会删除停止标志。根据终端提示修正 MuMu、Root 或 OPPO 健康状态后，重复执行恢复命令即可。

#### 突然断电或来不及暂停

如果电脑已经死机、断电或强制重启，无法提前执行暂停脚本，开机后仍按“开机后恢复采集”的步骤操作。恢复脚本会把没有对应存活进程的旧 `running` 批次改为 `interrupted`，再从最后一次成功提交的 checkpoint 继续。最坏情况下只会重新采集最后一个未完整提交的日期；已完成历史日期、原始证据和旧版本不会被覆盖。

## 统一健康 Web

Web 应用只读取本机 SQLite 和内容寻址素材，不会修改健康数据、控制模拟器或把数据发送到外网。默认仅供这台电脑访问：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_web.ps1
```

也可直接运行：

```powershell
python .\ohealth_exporter.py web --open
```

浏览器会打开 `http://127.0.0.1:8765/`。页面包括：

- 健康总览：按 7/30/90 天、1/2/3 年或 Life span（全部历史）分别浏览步数、消耗、锻炼时长、活动次数、心率、睡眠、血氧、血压、体重和压力；
- 指标详情：所有指标支持相同时间跨度；步数、消耗、锻炼时长和活动次数提供范围总计、日均、单日最高、长期日级趋势及每日目标/明细，其他指标展示范围平均、最低、最高以及单日精细采样、睡眠阶段、评分和页面证据；
- 数据来源：厂商、APP 版本、脱敏设备、数据范围和来源优先级；
- 数据质量：保真等级、无数据日期、精细序列覆盖与未解决验真差异；
- 采集管理：ADB/Root/APP 状态、checkpoint、详细日志和每秒操作事件。

总览以历史统计为主：每张指标卡的主数字是所选时间段平均值，同时展示最小值、中位数、最大值、实际有记录日和统计起止日期；最新单日值只在指标详情中查看。Web 会明确区分“数值为 0”“没有记录”和“只有日汇总”。如果历史日期缺少精细序列，页面会展示缺口说明，不会根据截图高度估算数值。Life span 的统计值始终使用全部日记录精确计算；趋势图采用保留峰值和整体形状的 LTTB 降采样，并显示原始点数和展示点数，避免多年数据导致浏览器卡顿。“查看 APP 页面证据”会列出该日全部去重后的截图；点击缩略图可全屏查看、滚动长图、缩放、切换前后图片、下载原图或在新窗口打开。

### 手机和平板局域网访问

只有需要同一局域网内的手机或平板访问时才开启 `-Lan`：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_web.ps1 -Lan
```

首次启动会在终端要求设置至少 8 位的访问密码。密码使用随机盐和 `scrypt` 保存为本地哈希，浏览器登录后使用 HttpOnly 会话 Cookie；服务重启后需重新登录。手机访问地址为 `http://<这台电脑的局域网IP>:8765/`。只应在可信家庭局域网使用，不要进行公网端口映射或隧道转发。

需要重新设置密码时：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_web.ps1 -Lan -ResetPassword
```

### 本地可视化监控与调试日志

原有监控命令继续兼容，并会直接进入新 Web 的“采集管理”页面：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_dashboard.ps1
```

浏览器会打开 `http://127.0.0.1:8765/admin/collector`。页面每秒刷新，展示 ADB/Root/APP/采集进程状态、各指标 checkpoint、未解决失败、素材数量和容量、实时事件流以及可选择的完整日志尾部。中文 Windows PowerShell 日志会自动识别 UTF-8、UTF-16 或 GB18030 编码。

“实时详细操作”单独读取 `data/logs/operation-events.jsonl`。采集器会为进入指标、读取日期、打开/滚动日历、点击日期、逐屏滚动、保存 PNG/XML、进入和退出子页面、提交页面/checkpoint、回退及失败分别写入结构化事件。每条事件包含本地毫秒时间戳、`DEBUG/INFO/WARNING/ERROR` 级别、run ID、指标、日期、当前页面/Activity、动作、结果和附加上下文。页面支持级别筛选、100/300/1000 条尾部范围和自动滚到底部；选择 1 秒即为最快实时刷新。

也可直接运行：

```powershell
python .\ohealth_exporter.py dashboard --open
```

`dashboard` 兼容命令始终只监听本机。局域网访问必须使用上面的 `web --lan` 安全入口。

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

## 隐私、安全与责任边界

- 本项目默认仅在本机处理数据，不集成遥测、广告、第三方统计或 CDN。依赖安装和可选 Frida Server 下载会访问软件包仓库或 Frida 官方 GitHub Release；健康数据不会因此上传。完整的数据分类、保留和删除建议见 [PRIVACY.md](PRIVACY.md)。
- Web 默认只监听 `127.0.0.1`，并校验本机/私网 Host 以降低 DNS rebinding 风险。`--lan` 使用密码和 HttpOnly Cookie，但仍是明文 HTTP，只适合可信家庭局域网；不要直接暴露到公网。漏洞请按 [SECURITY.md](SECURITY.md) 私下报告，切勿附带真实数据库、日志、截图、密钥或盐值。
- Root、ADB 和 Frida 只应在你拥有或获明确授权的设备、账号和数据上使用。你需要自行确认当地法律、应用条款、设备保修和组织政策；本项目不提供绕过账号授权、Root 检测或访问他人数据的功能。
- 本项目不是医疗器械，展示结果不能替代医生诊断或紧急医疗服务。导出前后都应保留厂商原始数据，并校验备份可恢复性。
- OPPO、OPlus、HeyTap、MuMu、小米和华为等名称及商标归各自权利人所有。本项目是独立的社区项目，与这些厂商不存在隶属、授权或背书关系。

项目代码按 [MIT License](LICENSE) 发布；前端编译产物包含 Apache ECharts、React、zrender、Lucide 等第三方组件，归属和再分发条款见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。

当前公开发布门禁和已验证结果见 [AUDIT_REPORT.md](AUDIT_REPORT.md)；完整命令、发行包检查和 GitHub 设置见 [PUBLIC_RELEASE.md](PUBLIC_RELEASE.md)。任何标为阻断的历史项解决前都不应把仓库设为 Public。

## 开发验证

```powershell
python -m pytest
python -m compileall -q health_exporter ohealth_exporter.py
python -m ruff check health_exporter tests scripts ohealth_exporter.py
python -m bandit -r health_exporter ohealth_exporter.py -x health_exporter/web_dist -ll
python -m pip_audit . --progress-spinner off
python .\scripts\public_release_check.py
python .\scripts\license_compliance_check.py
cd .\webapp
npm ci
npm test
npm audit --omit=dev --audit-level=moderate
npm run build
```

生产前端会编译到 `health_exporter/web_dist/`，由同一个本地 Python 服务提供；运行时不需要单独启动 Node。首次公开发布或改写历史后还要运行 `python .\scripts\public_release_check.py --history`；该检查也会拒绝未审核的历史作者/提交者名称和非 GitHub noreply 邮箱。
