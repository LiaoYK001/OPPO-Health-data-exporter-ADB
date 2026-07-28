[CmdletBinding()]
param(
    [string]$Workspace,
    [int]$Port = 8765,
    [switch]$Lan,
    [switch]$ResetPassword,
    [switch]$NoOpen
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

$Arguments = @(
    '.\ohealth_exporter.py',
    'web',
    '--port', $Port
)
if ($Lan) {
    $Arguments += '--lan'
}
if ($ResetPassword) {
    if (-not $Lan) {
        throw '-ResetPassword 必须与 -Lan 一起使用。'
    }
    $Arguments += '--reset-password'
}
if (-not $NoOpen) {
    $Arguments += '--open'
}

& python @Arguments
exit $LASTEXITCODE
