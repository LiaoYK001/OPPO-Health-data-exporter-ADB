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
$DatabasePath = Join-Path $DataDir 'health.sqlite3'
$StopFile = Join-Path $DataDir 'STOP_UI_COLLECTION'

if (-not (Test-Path -LiteralPath $DatabasePath)) {
    throw "未找到采集数据库：$DatabasePath"
}

function Get-ProjectProcesses {
    Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessId -ne $PID -and $_.CommandLine -and (
            $_.CommandLine -match 'continue_after_heart\.ps1' -or
            ($_.CommandLine -match 'ohealth_exporter\.py' -and $_.CommandLine -match '(?i)(^|\s)collect(\s|$)')
        )
    }
}

$Processes = @(Get-ProjectProcesses)
$Supervisors = @($Processes | Where-Object { $_.CommandLine -match 'continue_after_heart\.ps1' })
$Collectors = @($Processes | Where-Object { $_.CommandLine -match 'ohealth_exporter\.py' })

# Create the supervisor stop marker only after process discovery succeeds, but
# before stopping anything, so the supervisor cannot launch a replacement.
New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
New-Item -ItemType File -Path $StopFile -Force | Out-Null

foreach ($Process in $Supervisors) {
    Write-Host "停止续跑调度器 PID $($Process.ProcessId) ..."
    Stop-Process -Id $Process.ProcessId -Force -ErrorAction SilentlyContinue
}
foreach ($Process in $Collectors) {
    Write-Host "停止页面采集器 PID $($Process.ProcessId) ..."
    Stop-Process -Id $Process.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Milliseconds 800

$PythonCommand = Get-Command python -ErrorAction Stop
$MarkInterrupted = @'
import datetime as dt
import sqlite3

db = sqlite3.connect("data/health.sqlite3", timeout=30)
now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
db.execute(
    """
    UPDATE ingest_runs
       SET status = 'interrupted',
           finished_at = COALESCE(finished_at, ?),
           error = COALESCE(error, 'planned host restart')
     WHERE command = 'collect' AND status = 'running'
    """,
    (now,),
)
db.commit()

rows = db.execute(
    """
    SELECT canonical_metric, last_date, state_json, updated_at
      FROM checkpoints
     ORDER BY canonical_metric
    """
).fetchall()
for row in rows:
    print("CHECKPOINT\t" + "\t".join("" if value is None else str(value) for value in row))
db.close()
'@
$MarkInterrupted | & $PythonCommand.Source -
if ($LASTEXITCODE -ne 0) {
    throw '无法把正在运行的采集批次标记为 interrupted；请不要重启，并把上面的错误发给 Codex。'
}

$Remaining = @(Get-ProjectProcesses)
if ($Remaining.Count -gt 0) {
    $Ids = ($Remaining | ForEach-Object { $_.ProcessId }) -join ', '
    throw "仍有项目采集进程未退出（PID：$Ids），请不要立即重启。"
}

Write-Host ''
Write-Host '采集已安全暂停，checkpoint 已保存在 data\health.sqlite3。' -ForegroundColor Green
Write-Host '现在可以重启电脑。请保留 STOP_UI_COLLECTION 文件，续跑脚本会在检查通过后自动移除它。'
