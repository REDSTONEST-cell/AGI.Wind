param([int]$Port = 18440)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'http-local.ps1')
$pythonExe = Join-Path $projectRoot 'app\.venv\Scripts\python.exe'
$stateDir = Join-Path $projectRoot 'data'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'Run the project dependency setup first; see docs/ops/runbook.md.' }
& $pythonExe (Join-Path $projectRoot 'scripts\setup-dev-postgres.py')
if ($LASTEXITCODE -ne 0) { throw 'Project PostgreSQL startup failed.' }
& $pythonExe -m agi_wind.cli --root $projectRoot init-local
if ($LASTEXITCODE -ne 0) { throw 'Local initialization failed.' }
$pidFile = Join-Path $stateDir 'gateway.pid'
if (Test-Path -LiteralPath $pidFile) {
    $savedPid = [int](Get-Content -LiteralPath $pidFile -Raw)
    $existing = Get-CimInstance Win32_Process -Filter "ProcessId=$savedPid" -ErrorAction SilentlyContinue
    if ($existing -and $existing.CommandLine.Contains('agi_wind.cli') -and $existing.CommandLine.Contains($projectRoot)) {
        Write-Output "Gateway already running: http://127.0.0.1:$Port"
        exit 0
    }
}
$argsList = @('-m', 'agi_wind.cli', '--root', ('"' + $projectRoot + '"'), 'serve', '--port', "$Port")
$process = Start-Process -FilePath $pythonExe -ArgumentList $argsList -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $stateDir 'gateway.stdout.log') -RedirectStandardError (Join-Path $stateDir 'gateway.stderr.log') -PassThru
[IO.File]::WriteAllText($pidFile, "$($process.Id)")
[IO.File]::WriteAllText((Join-Path $stateDir 'gateway-port.txt'), "$Port")
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Milliseconds 300
    try {
        $result = Invoke-LocalHttp -Uri "http://127.0.0.1:$Port/health/live" -TimeoutMs 2000
        if ($result.StatusCode -eq 200 -and ($result.Body | ConvertFrom-Json).status -eq 'live') { Write-Output "Gateway running: http://127.0.0.1:$Port"; exit 0 }
    } catch {}
    if ($process.HasExited) { throw 'Gateway exited; inspect data/gateway.stderr.log.' }
}
throw 'Gateway did not become live; inspect data/gateway.stderr.log.'
