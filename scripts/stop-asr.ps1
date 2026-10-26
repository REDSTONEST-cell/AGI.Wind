param()
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pidFile = Join-Path $projectRoot 'data\asr.pid'
$serverScript = Join-Path $PSScriptRoot 'asr_server.py'
if (Test-Path -LiteralPath $pidFile) {
    $savedAsrId = [int](Get-Content -LiteralPath $pidFile -Raw)
    $existing = Get-CimInstance Win32_Process -Filter "ProcessId=$savedAsrId" -ErrorAction SilentlyContinue
    if ($existing -and $existing.CommandLine -and $existing.CommandLine.Contains($serverScript) -and $existing.CommandLine.Contains($projectRoot)) {
        Stop-Process -Id $savedAsrId
        Write-Output '[ASR] 已停止项目识别服务。'
    } elseif ($existing) { throw 'ASR PID 不属于本项目，未停止。' }
    Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue
}
