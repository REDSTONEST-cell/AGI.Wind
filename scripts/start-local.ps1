param([int]$Port = 18440, [string]$BindHost = '127.0.0.1')
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'http-local.ps1')
$pythonExe = Join-Path $projectRoot 'app\.venv\Scripts\python.exe'
$stateDir = Join-Path $projectRoot 'data'
$scriptDir = $PSScriptRoot

function Start-LocalVoice {
    $voiceScript = Join-Path $scriptDir 'start-voice.ps1'
    if (-not (Test-Path -LiteralPath $voiceScript)) {
        Write-Output '未找到 scripts/start-voice.ps1，跳过本地语音启动。'
        return
    }
    try { & $voiceScript } catch { Write-Output "语音服务启动失败，网关继续运行：$($_.Exception.Message)" }
}

if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'Run the project dependency setup first; see docs/ops/runbook.md.' }
& $pythonExe (Join-Path $projectRoot 'scripts\setup-dev-postgres.py')
if ($LASTEXITCODE -ne 0) { throw 'Project PostgreSQL startup failed.' }
& $pythonExe -m agi_wind.cli --root $projectRoot init-local
if ($LASTEXITCODE -ne 0) { throw 'Local initialization failed.' }
$pidFile = Join-Path $stateDir 'gateway.pid'
if (Test-Path -LiteralPath $pidFile) {
    $savedPid = [int](Get-Content -LiteralPath $pidFile -Raw)
    $existing = Get-CimInstance Win32_Process -Filter "ProcessId=$savedPid" -ErrorAction SilentlyContinue
    $owned = $existing -and $existing.CommandLine.Contains('agi_wind.cli') -and $existing.CommandLine.Contains($projectRoot)
    $existingLive = $false
    if ($owned) {
        for ($probe = 0; $probe -lt 10; $probe++) {
            try {
                $reply = Invoke-LocalHttp -Uri "http://127.0.0.1:$Port/health/live" -TimeoutMs 1500
                if ($reply.StatusCode -eq 200 -and ($reply.Body | ConvertFrom-Json).status -eq 'live') { $existingLive = $true; break }
            } catch {}
            Start-Sleep -Milliseconds 500
        }
    }
    if ($owned -and $existingLive) {
        Write-Output "Gateway already running: http://127.0.0.1:$Port"
        Start-LocalVoice
        exit 0
    }
    if ($owned) {
        # A verified project process that does not answer /health/live cannot be reused; leaving it
        # would make the next start appear successful while the port is still held.
        Write-Output "Stale Gateway process $savedPid is not live; stopping the verified project process before restart."
        $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$savedPid" -ErrorAction SilentlyContinue
        foreach ($child in @($children)) {
            if ($child -and $child.CommandLine -and $child.CommandLine.Contains('agi_wind.cli')) {
                Stop-Process -Id $child.ProcessId -Force -ErrorAction SilentlyContinue
            }
        }
        Stop-Process -Id $savedPid -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    }
}
$argsList = @('-m', 'agi_wind.cli', '--root', ('"' + $projectRoot + '"'), 'serve', '--port', "$Port", '--host', $BindHost)
$process = Start-Process -FilePath $pythonExe -ArgumentList $argsList -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $stateDir 'gateway.stdout.log') -RedirectStandardError (Join-Path $stateDir 'gateway.stderr.log') -PassThru
[IO.File]::WriteAllText($pidFile, "$($process.Id)")
[IO.File]::WriteAllText((Join-Path $stateDir 'gateway-port.txt'), "$Port")
[IO.File]::WriteAllText((Join-Path $stateDir 'gateway-host.txt'), $BindHost)
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Milliseconds 300
    try {
        $result = Invoke-LocalHttp -Uri "http://127.0.0.1:$Port/health/live" -TimeoutMs 2000
        if ($result.StatusCode -eq 200 -and ($result.Body | ConvertFrom-Json).status -eq 'live') {
            if ($BindHost -in @('127.0.0.1', 'localhost', '::1')) {
                Write-Output "Gateway running: http://127.0.0.1:$Port"
            } else {
                Write-Output "Gateway running: http://${BindHost}:$Port (LAN mode; client key authentication required)"
            }
            Start-LocalVoice
            exit 0
        }
    } catch {}
    if ($process.HasExited) { throw 'Gateway exited; inspect data/gateway.stderr.log.' }
}
throw 'Gateway did not become live; inspect data/gateway.stderr.log.'
