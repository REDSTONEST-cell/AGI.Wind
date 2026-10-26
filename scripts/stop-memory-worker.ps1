$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pidFile = Join-Path $projectRoot 'data\memory-worker.pid'
if (-not (Test-Path -LiteralPath $pidFile)) { return }
$savedPid = [int](Get-Content -LiteralPath $pidFile -Raw)
$process = Get-CimInstance Win32_Process -Filter "ProcessId=$savedPid" -ErrorAction SilentlyContinue
if ($process) {
    if (-not $process.CommandLine -or -not $process.CommandLine.Contains('agi_wind.cli') -or -not $process.CommandLine.Contains('memory-worker') -or -not $process.CommandLine.Contains($projectRoot)) { throw 'PID is not owned by this memory worker; refusing to stop it.' }
    Stop-Process -Id $savedPid
}
Remove-Item -LiteralPath $pidFile
Write-Output 'Memory worker stopped; existing paid requests may have unknown usage bounds.'
