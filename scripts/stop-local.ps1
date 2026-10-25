param([switch]$StopDatabase)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'http-local.ps1')
$stateDir = Join-Path $projectRoot 'data'
$portFile = Join-Path $stateDir 'gateway-port.txt'
$port = if (Test-Path -LiteralPath $portFile) { [int](Get-Content -LiteralPath $portFile -Raw) } else { 18440 }
$tokenPath = Join-Path $stateDir 'access-token.txt'
if (Test-Path -LiteralPath $tokenPath) {
    $token = (Get-Content -LiteralPath $tokenPath -Raw).Trim()
    try {
        $reply = Invoke-LocalHttp -Method Post -Uri "http://127.0.0.1:$port/api/v1/admin/shutdown" -Headers @{Authorization="Bearer $token"} -TimeoutMs 5000
        if ($reply.StatusCode -eq 200) { Write-Output 'Gateway graceful shutdown requested.' } else { throw 'Gateway refused managed shutdown.' }
    } catch { Write-Output 'Gateway is not reachable or refused shutdown; no unrelated process was stopped.' }
    $token = $null
}
if ($StopDatabase) {
    $pidFile = Join-Path $stateDir 'gateway.pid'
    if (Test-Path -LiteralPath $pidFile) {
        $savedPid = [int](Get-Content -LiteralPath $pidFile -Raw)
        $process = Get-Process -Id $savedPid -ErrorAction SilentlyContinue
        if ($process -and -not $process.WaitForExit(10000)) { throw 'Gateway has not exited; database left running.' }
    }
    $pgCtl = Join-Path $projectRoot '.devtools\pgsql\bin\pg_ctl.exe'
    $pgData = Join-Path $stateDir 'dev-postgres'
    if (Test-Path -LiteralPath (Join-Path $pgData 'PG_VERSION')) { & $pgCtl -D $pgData -m fast -w stop }
}
