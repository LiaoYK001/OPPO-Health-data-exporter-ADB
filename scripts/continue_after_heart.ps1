[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [int]$HeartProcessId,

    [string]$Workspace = (Split-Path -Parent $PSScriptRoot),

    [ValidateRange(0, 10)]
    [int]$MaxHeartRestarts = 3
)

$ErrorActionPreference = 'Stop'
$Workspace = (Resolve-Path -LiteralPath $Workspace).Path
$LogDir = Join-Path $Workspace 'data\logs'
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$SupervisorLog = Join-Path $LogDir 'ui-history-supervisor.log'
$StopFile = Join-Path $Workspace 'data\STOP_UI_COLLECTION'

function Write-SupervisorLog {
    param([string]$Message)
    $line = '{0} {1}' -f (Get-Date).ToString('o'), $Message
    Add-Content -LiteralPath $SupervisorLog -Value $line -Encoding UTF8
}

function Get-LatestCollectStatus {
    $Probe = @'
import sqlite3

connection = sqlite3.connect("file:data/health.sqlite3?mode=ro", uri=True)
row = connection.execute(
    "SELECT status FROM ingest_runs WHERE command='collect' "
    "ORDER BY started_at DESC LIMIT 1"
).fetchone()
print(row[0] if row else "missing")
'@
    $Status = ($Probe | & $Python -).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw 'failed to read latest collect run status'
    }
    return $Status
}

Set-Location -LiteralPath $Workspace
$Python = (Get-Command python -ErrorAction Stop).Source
$CurrentHeartProcessId = $HeartProcessId
$HeartRestartCount = 0

while ($true) {
    $HeartProcess = Get-Process -Id $CurrentHeartProcessId -ErrorAction Stop
    Write-SupervisorLog "waiting for heart-rate collector pid=$CurrentHeartProcessId restart=$HeartRestartCount/$MaxHeartRestarts"
    $HeartProcess.WaitForExit()
    $HeartRunStatus = Get-LatestCollectStatus
    Write-SupervisorLog "heart-rate collector pid=$CurrentHeartProcessId exited run_status=$HeartRunStatus"

    if ($HeartRunStatus -eq 'complete') {
        break
    }
    if (Test-Path -LiteralPath $StopFile) {
        Write-SupervisorLog "stop marker present; collector will not restart: $StopFile"
        exit 1
    }
    if ($HeartRestartCount -ge $MaxHeartRestarts) {
        Write-SupervisorLog 'remaining metrics not started because heart-rate retry limit was reached'
        exit 1
    }

    $InterruptedAt = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    $MarkInterrupted = @"
import sqlite3

connection = sqlite3.connect("data/health.sqlite3")
connection.execute(
    "UPDATE ingest_runs SET status='interrupted', finished_at=?, "
    "error=COALESCE(error, 'collector process exited before run finalization') "
    "WHERE command='collect' AND status='running'",
    ("$InterruptedAt",),
)
connection.commit()
"@
    $MarkInterrupted | & $Python -
    if ($LASTEXITCODE -ne 0) {
        Write-SupervisorLog 'failed to mark abandoned collector run; automatic restart cancelled'
        exit $LASTEXITCODE
    }

    $HeartRestartCount += 1
    $Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $HeartStdout = Join-Path $LogDir "heart-history-retry-$Stamp.out.log"
    $HeartStderr = Join-Path $LogDir "heart-history-retry-$Stamp.err.log"
    $HeartArguments = @(
        '.\ohealth_exporter.py',
        'collect',
        '--ui-only',
        '--metrics',
        'heart_rate',
        '--all-history',
        '--resume',
        '--max-scrolls',
        '20'
    )
    Write-SupervisorLog "restarting heart-rate collector attempt=$HeartRestartCount; stdout=$HeartStdout stderr=$HeartStderr"
    $HeartProcess = Start-Process -FilePath $Python -ArgumentList $HeartArguments `
        -WorkingDirectory $Workspace -WindowStyle Hidden `
        -RedirectStandardOutput $HeartStdout -RedirectStandardError $HeartStderr `
        -PassThru
    $CurrentHeartProcessId = $HeartProcess.Id
}

$CheckpointProbe = @'
import sqlite3

connection = sqlite3.connect("file:data/health.sqlite3?mode=ro", uri=True)
source_row = connection.execute(
    "SELECT source_id FROM ingest_runs WHERE command='collect' ORDER BY started_at DESC LIMIT 1"
).fetchone()
if source_row is None:
    raise SystemExit("heart-rate source is missing")
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
if earliest is None or checkpoint != earliest:
    raise SystemExit(
        f"heart-rate checkpoint incomplete: checkpoint={checkpoint or 'missing'} "
        f"earliest={earliest or 'missing'}"
    )
'@

$CheckpointProbe | & $Python -
if ($LASTEXITCODE -ne 0) {
    Write-SupervisorLog 'remaining metrics not started because heart-rate checkpoint is incomplete'
    exit $LASTEXITCODE
}

$RepairStdout = Join-Path $LogDir 'repair-normalized.out.log'
$RepairStderr = Join-Path $LogDir 'repair-normalized.err.log'
Write-SupervisorLog "repairing audited normalized fields; stdout=$RepairStdout stderr=$RepairStderr"
& $Python '.\ohealth_exporter.py' 'repair-normalized' 1> $RepairStdout 2> $RepairStderr
if ($LASTEXITCODE -ne 0) {
    Write-SupervisorLog "normalized repair failed code=$LASTEXITCODE; remaining metrics not started"
    exit $LASTEXITCODE
}

$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$Stdout = Join-Path $LogDir "remaining-history-$Stamp.out.log"
$Stderr = Join-Path $LogDir "remaining-history-$Stamp.err.log"
$Arguments = @(
    '.\ohealth_exporter.py',
    'run',
    '--ui-only',
    '--metrics',
    'activity,sleep,spo2,blood_pressure,weight,stress',
    '--all-history',
    '--resume',
    '--max-scrolls',
    '20'
)

Write-SupervisorLog "starting remaining UI history; stdout=$Stdout stderr=$Stderr"
& $Python @Arguments 1> $Stdout 2> $Stderr
$RemainingExitCode = $LASTEXITCODE
Write-SupervisorLog "remaining UI history exited code=$RemainingExitCode"
exit $RemainingExitCode
