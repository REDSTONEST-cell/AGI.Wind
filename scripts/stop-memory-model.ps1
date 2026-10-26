param()
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pidFile = Join-Path $projectRoot 'data\memory-model.pid'
$identityFile = Join-Path $projectRoot 'data\memory-model.process.json'
$serverScript = Join-Path $PSScriptRoot 'memory-server.py'
$pythonExe = Join-Path $projectRoot '.devtools\memory-venv\Scripts\python.exe'
if (Test-Path -LiteralPath $pidFile) {
    $savedMemoryId = [int](Get-Content -LiteralPath $pidFile -Raw)
    $existing = Get-CimInstance Win32_Process -Filter "ProcessId=$savedMemoryId" -ErrorAction SilentlyContinue
    if ($existing) {
        if (-not (Test-Path -LiteralPath $identityFile)) { throw 'Missing memory process identity; no process was stopped.' }
        $identity = Get-Content -LiteralPath $identityFile -Raw | ConvertFrom-Json
        $process = Get-Process -Id $savedMemoryId -ErrorAction Stop
        if ($identity.pid -ne $savedMemoryId -or $process.StartTime.ToUniversalTime().ToString('o') -ne $identity.started -or -not $existing.CommandLine -or -not $existing.CommandLine.Contains($serverScript) -or -not $existing.CommandLine.Contains($projectRoot) -or $existing.ExecutablePath -ne $pythonExe) { throw 'Saved memory PID identity mismatch; no process was stopped.' }
        # Windows venv may run a child interpreter: only stop a child when both
        # ancestry and the exact project command match the owned parent process.
        Get-CimInstance Win32_Process -Filter "ParentProcessId=$savedMemoryId" -ErrorAction SilentlyContinue | ForEach-Object {
            if ($_.CommandLine -and $_.CommandLine.Contains($serverScript) -and $_.CommandLine.Contains($projectRoot)) { Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }
        }
        Stop-Process -Id $savedMemoryId -ErrorAction SilentlyContinue
        Write-Output '[Memory] Project CPU model service stopped.'
    }
    Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $identityFile -ErrorAction SilentlyContinue
}
