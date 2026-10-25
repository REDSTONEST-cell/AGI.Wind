$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'http-local.ps1')
$portFile = Join-Path $projectRoot 'data\gateway-port.txt'
$port = if (Test-Path -LiteralPath $portFile) { [int](Get-Content -LiteralPath $portFile -Raw) } else { 18440 }
try {
    $reply = Invoke-LocalHttp -Uri "http://127.0.0.1:$port/health/live"
    $live = $reply.Body | ConvertFrom-Json
    Write-Output "Gateway: $($live.status) on http://127.0.0.1:$port"
    $ready = Invoke-LocalHttp -Uri "http://127.0.0.1:$port/health/ready"
    Write-Output $ready.Body
} catch { Write-Output 'Gateway is not running.'; exit 1 }
