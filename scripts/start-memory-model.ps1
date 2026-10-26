param()
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'http-local.ps1')
$pythonExe = Join-Path $projectRoot '.devtools\memory-venv\Scripts\python.exe'
$serverScript = Join-Path $PSScriptRoot 'memory-server.py'
$stateDir = Join-Path $projectRoot 'data'
$pidFile = Join-Path $stateDir 'memory-model.pid'
$tokenPath = Join-Path $stateDir 'memory-token.txt'
$configPath = Join-Path $projectRoot 'config\memory.local.json'
if (-not (Test-Path -LiteralPath $pythonExe) -or -not (Test-Path -LiteralPath $configPath) -or -not (Test-Path -LiteralPath $tokenPath)) {
    Write-Output '[Memory] Local semantic model is not installed. Run scripts/setup-memory-model.ps1.'
    return
}
$headers = @{ Authorization = ('Bearer ' + [IO.File]::ReadAllText($tokenPath).Trim()) }
$expectedFingerprint = ([IO.File]::ReadAllText($configPath) | ConvertFrom-Json).fingerprint
if (Test-Path -LiteralPath $pidFile) {
    $savedMemoryId = [int](Get-Content -LiteralPath $pidFile -Raw)
    $existing = Get-CimInstance Win32_Process -Filter "ProcessId=$savedMemoryId" -ErrorAction SilentlyContinue
    if ($existing -and $existing.CommandLine) {
        if (-not $existing.CommandLine.Contains($serverScript) -or -not $existing.CommandLine.Contains($projectRoot) -or $existing.ExecutablePath -ne $pythonExe) { throw 'Saved memory PID belongs to another process; no process was changed.' }
        $reply = Invoke-LocalHttp -Uri 'http://127.0.0.1:8141/health' -Headers $headers -TimeoutMs 1000
        if ($reply.StatusCode -eq 200 -and ($reply.Body | ConvertFrom-Json).fingerprint -eq $expectedFingerprint) { Write-Output '[Memory] Local CPU model is already ready on 127.0.0.1:8141.'; return }
        throw 'Project memory process exists but is not ready. Check data/memory-model.stderr.log.'
    }
}
if (Get-NetTCPConnection -State Listen -LocalPort 8141 -ErrorAction SilentlyContinue) { throw 'Port 8141 is occupied; no process was changed.' }
$process = Start-Process -FilePath $pythonExe -ArgumentList @('-u', ('"'+$serverScript+'"'), '--root', ('"'+$projectRoot+'"')) -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $stateDir 'memory-model.stdout.log') -RedirectStandardError (Join-Path $stateDir 'memory-model.stderr.log') -PassThru
$identity = @{ pid = $process.Id; started = $process.StartTime.ToUniversalTime().ToString('o') }
[IO.File]::WriteAllText($pidFile, "$($process.Id)")
$identity | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stateDir 'memory-model.process.json') -Encoding UTF8
for ($attempt = 0; $attempt -lt 120; $attempt++) {
    Start-Sleep -Milliseconds 250
    if ($process.HasExited) { throw 'Memory service exited. Check data/memory-model.stderr.log.' }
    try {
        $reply = Invoke-LocalHttp -Uri 'http://127.0.0.1:8141/health' -Headers $headers -TimeoutMs 500
        if ($reply.StatusCode -eq 200 -and ($reply.Body | ConvertFrom-Json).fingerprint -eq $expectedFingerprint) { Write-Output '[Memory] Local CPU model is ready on 127.0.0.1:8141 (2 threads).'; return }
    } catch {}
}
throw 'Memory model startup timed out. Check data/memory-model.stderr.log.'
