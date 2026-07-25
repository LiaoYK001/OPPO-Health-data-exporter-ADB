[CmdletBinding()]
param(
    [string]$Workspace
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$ScriptDirectory = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($ScriptDirectory)) {
    $ScriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if ([string]::IsNullOrWhiteSpace($Workspace)) {
    $Workspace = Split-Path -Parent $ScriptDirectory
}
$Workspace = (Resolve-Path -LiteralPath $Workspace).Path
Set-Location -LiteralPath $Workspace

$DataDir = Join-Path $Workspace 'data'
$LogDir = Join-Path $DataDir 'logs'
$DatabasePath = Join-Path $DataDir 'health.sqlite3'
$StopFile = Join-Path $DataDir 'STOP_UI_COLLECTION'
$CollectorScript = Join-Path $Workspace 'ohealth_exporter.py'
$SupervisorScript = Join-Path $ScriptDirectory 'continue_after_heart.ps1'

foreach ($RequiredPath in @($DatabasePath, $CollectorScript, $SupervisorScript)) {
    if (-not (Test-Path -LiteralPath $RequiredPath)) {
        throw "缺少续跑所需文件：$RequiredPath"
    }
}

$Existing = @(Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne $PID -and $_.CommandLine -and (
        $_.CommandLine -match 'continue_after_heart\.ps1' -or
        ($_.CommandLine -match 'ohealth_exporter\.py' -and $_.CommandLine -match '(?i)(^|\s)collect(\s|$)')
    )
})
if ($Existing.Count -gt 0) {
    $Ids = ($Existing | ForEach-Object { $_.ProcessId }) -join ', '
    throw "检测到采集已经在运行（PID：$Ids）。为避免重复启动，本脚本已退出。"
}

$PythonCommand = Get-Command python -ErrorAction Stop

Write-Host '检查 ADB、MuMu、OPPO 健康前台状态和 Root 状态 ...'
& $PythonCommand.Source $CollectorScript doctor --phase ui
if ($LASTEXITCODE -ne 0) {
    throw @'
UI 采集前置检查未通过，尚未启动采集。
请确认：MuMu 已启动；Root 已关闭；OPPO 健康已打开并停留在“健康”首页；页面已经加载完成。
修正后重新运行本脚本即可。
'@
}

# After an unplanned restart the old process is gone but its run can still be
# marked running. Checkpoints are committed independently, so repair bookkeeping.
$RepairStaleRun = @'
import datetime as dt
import sqlite3

db = sqlite3.connect("data/health.sqlite3", timeout=30)
now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
db.execute(
    """
    UPDATE ingest_runs
       SET status = 'interrupted',
           finished_at = COALESCE(finished_at, ?),
           error = COALESCE(error, 'host restart detected during resume')
     WHERE command = 'collect' AND status = 'running'
    """,
    (now,),
)
db.commit()
checkpoint = db.execute(
    """
    SELECT last_date, updated_at
      FROM checkpoints
     WHERE canonical_metric = 'heart_rate'
     ORDER BY source_id DESC
     LIMIT 1
    """
).fetchone()
if checkpoint:
    print(f"HEART_CHECKPOINT\t{checkpoint[0]}\t{checkpoint[1]}")
db.close()
'@
$RepairStaleRun | & $PythonCommand.Source -
if ($LASTEXITCODE -ne 0) {
    throw '无法修复重启前遗留的运行状态，未启动采集。'
}

if (Test-Path -LiteralPath $StopFile) {
    Remove-Item -LiteralPath $StopFile -Force
}
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

$HeartCompleteProbe = @'
import sqlite3

connection = sqlite3.connect("file:data/health.sqlite3?mode=ro", uri=True)
source_row = connection.execute(
    "SELECT id FROM sources WHERE vendor='oppo' ORDER BY id DESC LIMIT 1"
).fetchone()
complete = False
if source_row:
    source_id = source_row[0]
    checkpoint_row = connection.execute(
        "SELECT last_date FROM checkpoints WHERE source_id=? AND canonical_metric='heart_rate'",
        (source_id,),
    ).fetchone()
    earliest_row = connection.execute(
        "SELECT MIN(local_date) FROM metric_records "
        "WHERE source_id=? AND canonical_metric='heart_rate'",
        (source_id,),
    ).fetchone()
    checkpoint = checkpoint_row[0] if checkpoint_row else None
    earliest = earliest_row[0] if earliest_row else None
    complete = earliest is not None and checkpoint == earliest
print("complete" if complete else "incomplete")
'@
$HeartCheckpointState = ($HeartCompleteProbe | & $PythonCommand.Source -).Trim()
if ($LASTEXITCODE -ne 0) {
    throw '无法判断心率历史是否已经完成，未启动采集。'
}
$HeartAlreadyComplete = $HeartCheckpointState -eq 'complete'

$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$HeartOut = Join-Path $LogDir "heart-history-resume-$Stamp.out.log"
$HeartErr = Join-Path $LogDir "heart-history-resume-$Stamp.err.log"
$SupervisorOut = Join-Path $LogDir "supervisor-resume-$Stamp.out.log"
$SupervisorErr = Join-Path $LogDir "supervisor-resume-$Stamp.err.log"

$HeartProcess = $null
if (-not $HeartAlreadyComplete) {
    $CollectorArguments = @(
        $CollectorScript,
        'collect',
        '--ui-only',
        '--metrics', 'heart_rate',
        '--all-history',
        '--resume',
        '--max-scrolls', '20'
    )
    $HeartStart = @{
        FilePath = $PythonCommand.Source
        ArgumentList = $CollectorArguments
        WorkingDirectory = $Workspace
        WindowStyle = 'Hidden'
        RedirectStandardOutput = $HeartOut
        RedirectStandardError = $HeartErr
        PassThru = $true
    }
    $HeartProcess = Start-Process @HeartStart
}

try {
    $SupervisorArguments = @(
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-File', ('"' + $SupervisorScript + '"'),
        '-Workspace', ('"' + $Workspace + '"')
    )
    if ($HeartAlreadyComplete) {
        $SupervisorArguments += '-HeartAlreadyComplete'
    } else {
        $SupervisorArguments += @('-HeartProcessId', $HeartProcess.Id)
    }
    $SupervisorStart = @{
        FilePath = 'powershell.exe'
        ArgumentList = $SupervisorArguments
        WorkingDirectory = $Workspace
        WindowStyle = 'Hidden'
        RedirectStandardOutput = $SupervisorOut
        RedirectStandardError = $SupervisorErr
        PassThru = $true
    }
    $SupervisorProcess = Start-Process @SupervisorStart
} catch {
    if ($null -ne $HeartProcess) {
        Stop-Process -Id $HeartProcess.Id -Force -ErrorAction SilentlyContinue
    }
    throw
}

Start-Sleep -Seconds 2
if (-not (Get-Process -Id $SupervisorProcess.Id -ErrorAction SilentlyContinue)) {
    if ($null -ne $HeartProcess) {
        Stop-Process -Id $HeartProcess.Id -Force -ErrorAction SilentlyContinue
    }
    throw "续跑调度器启动后立即退出，请查看：$SupervisorOut 和 $SupervisorErr"
}

Write-Host ''
Write-Host '页面采集已从 SQLite checkpoint 继续。' -ForegroundColor Green
if ($HeartAlreadyComplete) {
    Write-Host '心率历史已到最早来源日期，已跳过重复采集。' -ForegroundColor Green
} else {
    Write-Host "心率采集 PID：$($HeartProcess.Id)"
    Write-Host "心率日志：$HeartOut"
}
Write-Host "后续指标调度 PID：$($SupervisorProcess.Id)"
Write-Host "调度日志：$SupervisorOut"
Write-Host '请保持 MuMu、OPPO 健康、电脑网络和当前显示设置不变；Root 必须继续关闭。'
