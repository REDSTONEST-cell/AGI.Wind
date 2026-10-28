param([ValidateRange(1,65535)][int]$Port = 18440, [switch]$Elevated, [switch]$DryRun)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$stateDir = Join-Path $projectRoot 'data'
$logPath = Join-Path $stateDir 'agent-admin-restart.log'

# Verification hook: resolve the restart target without UAC, stopping or starting anything.
if ($DryRun) {
    $hostFile = Join-Path $stateDir 'gateway-host.txt'
    $portFile = Join-Path $stateDir 'gateway-port.txt'
    $bindHost = if (Test-Path -LiteralPath $hostFile) { (Get-Content -LiteralPath $hostFile -Raw).Trim() } else { '127.0.0.1' }
    $listenPort = if (Test-Path -LiteralPath $portFile) { [int](Get-Content -LiteralPath $portFile -Raw).Trim() } else { $Port }
    if (-not $bindHost) { $bindHost = '127.0.0.1' }
    if (-not $listenPort) { $listenPort = $Port }
    Write-Output "dry-run: host=$bindHost port=$listenPort python=$projectRoot\app\.venv\Scripts\python.exe"
    exit 0
}

if (-not $Elevated) {
    # Windows shows UAC; no password is stored and no policy is changed.
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $PSCommandPath + '"'), '-Port', "$Port", '-Elevated')
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -WindowStyle Hidden -ArgumentList $arguments
    exit
}
$logPath = Join-Path $stateDir 'agent-admin-restart.log'
function Write-Log([string]$message) {
    Add-Content -LiteralPath $logPath -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $message) -Encoding utf8
}
try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Administrator approval was not granted.' }

    # A restart must keep the bind host the user originally launched with; forcing
    # 127.0.0.1 would drop LAN/IPv6 access after the UAC approval round-trip.
    $hostFile = Join-Path $stateDir 'gateway-host.txt'
    $portFile = Join-Path $stateDir 'gateway-port.txt'
    $bindHost = if (Test-Path -LiteralPath $hostFile) { (Get-Content -LiteralPath $hostFile -Raw).Trim() } else { '127.0.0.1' }
    $listenPort = if (Test-Path -LiteralPath $portFile) { [int](Get-Content -LiteralPath $portFile -Raw).Trim() } else { $Port }
    if (-not $bindHost) { $bindHost = '127.0.0.1' }
    if (-not $listenPort) { $listenPort = $Port }
    Write-Log "restart requested: host=$bindHost port=$listenPort"

    & (Join-Path $PSScriptRoot 'stop-local.ps1') -KeepVoice | Out-Null

    $pythonExe = Join-Path $projectRoot 'app\.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'python environment not found' }
    $argsList = @('-m', 'nagi.cli', '--root', ('"' + $projectRoot + '"'), 'serve', '--port', "$listenPort", '--host', $bindHost)
    $process = Start-Process -FilePath $pythonExe -ArgumentList $argsList -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $stateDir 'gateway.stdout.log') -RedirectStandardError (Join-Path $stateDir 'gateway.stderr.log') -PassThru
    [IO.File]::WriteAllText((Join-Path $stateDir 'gateway.pid'), "$($process.Id)")
    [IO.File]::WriteAllText((Join-Path $stateDir 'gateway-port.txt'), "$listenPort")
    [IO.File]::WriteAllText((Join-Path $stateDir 'gateway-host.txt'), $bindHost)

    $probeHost = if ($bindHost -eq '::' -or $bindHost -eq '::1') { '[::1]' } else { '127.0.0.1' }
    $live = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 400
        try {
            $reply = Invoke-WebRequest -UseBasicParsing -Uri "http://${probeHost}:$listenPort/health/live" -TimeoutSec 2
            if ($reply.StatusCode -eq 200) { $live = $true; break }
        } catch {}
        if ($process.HasExited) { break }
    }
    if (-not $live) { throw "gateway did not become live on $bindHost`:$listenPort" }
    Write-Log "restart ok: host=$bindHost port=$listenPort pid=$($process.Id) elevated=True"
} catch {
    Write-Log "restart failed: $($_.Exception.Message)"
    throw
}
