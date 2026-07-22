[CmdletBinding()]
param(
    [string]$Workspace,
    [int]$Port = 8765,
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
    'dashboard',
    '--host', '127.0.0.1',
    '--port', $Port
)
if (-not $NoOpen) {
    $Arguments += '--open'
}
& python @Arguments
exit $LASTEXITCODE
