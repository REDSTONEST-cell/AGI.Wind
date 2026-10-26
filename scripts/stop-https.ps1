param([switch]$Staging)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$suffix = if ($Staging) { 'staging' } else { 'production' }
$stateDir = Join-Path $projectRoot 'data\https'
$pidFile = Join-Path $stateDir "caddy-$suffix.pid"
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Output 'No managed Caddy PID recorded.'; return }
$savedPid = [int](Get-Content -LiteralPath $pidFile -Raw)
$process = Get-CimInstance Win32_Process -Filter "ProcessId=$savedPid" -ErrorAction SilentlyContinue
if ($process) {
    $allowedExecutables = @((Join-Path $projectRoot '.devtools\caddy\caddy.exe'), (Join-Path $projectRoot '.devtools\caddy\caddy-alidns.exe'))
    $caddyFile = Join-Path $stateDir "Caddyfile.$suffix"
    if ($process.ExecutablePath -notin $allowedExecutables -or -not $process.CommandLine.Contains($caddyFile)) { throw 'PID does not belong to this HTTPS deployment; refusing to stop it.' }
    Stop-Process -Id $savedPid
}
Remove-Item -LiteralPath $pidFile
Write-Output "HTTPS proxy stopped ($suffix); Gateway and speech services remain running."
