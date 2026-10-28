$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$stateDir = Join-Path $projectRoot 'data'
$pidFile = Join-Path $stateDir 'memory-worker.pid'
$pythonExe = Join-Path $projectRoot 'app\.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $pidFile) {
    $savedPid = [int](Get-Content -LiteralPath $pidFile -Raw)
    $existing = Get-CimInstance Win32_Process -Filter "ProcessId=$savedPid" -ErrorAction SilentlyContinue
    if ($existing -and $existing.CommandLine -and $existing.CommandLine.Contains('nagi.cli') -and $existing.CommandLine.Contains('memory-worker') -and $existing.CommandLine.Contains($projectRoot)) {
        Write-Output "Memory worker already running (pid $savedPid)."; return
    }
}
$arguments = @('-m','nagi.cli','--root',('"' + $projectRoot + '"'),'memory-worker')
$process = Start-Process -FilePath $pythonExe -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $stateDir 'memory-worker.stdout.log') -RedirectStandardError (Join-Path $stateDir 'memory-worker.stderr.log') -PassThru
[IO.File]::WriteAllText($pidFile,[string]$process.Id)
try { $process.PriorityClass = 'BelowNormal' } catch {}
Start-Sleep -Seconds 1
if ($process.HasExited) { throw 'Memory worker exited; read data/memory-worker.stderr.log.' }
Write-Output "Memory worker started (pid $($process.Id))."
