param([switch]$StopDatabase, [switch]$KeepVoice)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'http-local.ps1')
$stateDir = Join-Path $projectRoot 'data'

function Get-VoiceModelPath {
    $configPath = Join-Path $projectRoot 'config\voice.local.json'
    if (-not (Test-Path -LiteralPath $configPath)) { return $null }
    try {
        $voiceConfig = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($null -eq $voiceConfig -or -not $voiceConfig.model) { return $null }
        $model = [string]$voiceConfig.model
        if (-not $model) { return $null }
        if ([System.IO.Path]::IsPathRooted($model)) { return $model }
        return (Join-Path $projectRoot $model)
    } catch { return $null }
}
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
$gatewayPidFile = Join-Path $stateDir 'gateway.pid'
if (Test-Path -LiteralPath $gatewayPidFile) {
    $savedGatewayPid = 0
    $gatewayPidText = (Get-Content -LiteralPath $gatewayPidFile -Raw).Trim()
    if ([int]::TryParse($gatewayPidText, [ref]$savedGatewayPid) -and $savedGatewayPid -gt 0) {
        $gatewayProcess = Get-Process -Id $savedGatewayPid -ErrorAction SilentlyContinue
        if ($gatewayProcess -and -not $gatewayProcess.WaitForExit(15000)) {
            $verified = Get-CimInstance Win32_Process -Filter "ProcessId=$savedGatewayPid" -ErrorAction SilentlyContinue
            if ($verified -and $verified.CommandLine -and $verified.CommandLine.Contains('nagi.cli') -and $verified.CommandLine.Contains($projectRoot)) {
                Write-Output "Gateway did not exit within 15s; forcing the verified project process (pid $savedGatewayPid)."
                $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$savedGatewayPid" -ErrorAction SilentlyContinue
                foreach ($child in @($children)) {
                    if ($child -and $child.CommandLine -and $child.CommandLine.Contains('nagi.cli')) {
                        Stop-Process -Id $child.ProcessId -Force -ErrorAction SilentlyContinue
                    }
                }
                Stop-Process -Id $savedGatewayPid -Force -ErrorAction SilentlyContinue
                Start-Sleep -Seconds 1
            } else {
                Write-Output "Gateway pid $savedGatewayPid is not a verified project process; leaving it running."
            }
        }
    }
}
if (-not $KeepVoice) {
    try { & (Join-Path $PSScriptRoot 'stop-asr.ps1') } catch { Write-Output "ASR 停止失败：$($_.Exception.Message)" }
    try {
        $voicePidFile = Join-Path $stateDir 'voice.pid'
        if (-not (Test-Path -LiteralPath $voicePidFile)) {
            Write-Output 'Voice: 未找到 data/voice.pid，跳过语音进程停止。'
        } else {
            $voicePid = 0
            $voicePidText = (Get-Content -LiteralPath $voicePidFile -Raw).Trim()
            if (-not [int]::TryParse($voicePidText, [ref]$voicePid) -or $voicePid -le 0) {
                Write-Output 'Voice: data/voice.pid 内容无效，保留相关进程不动。'
            } else {
                $voiceProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$voicePid" -ErrorAction SilentlyContinue
                $voiceModel = Get-VoiceModelPath
                if ($null -eq $voiceProcess) {
                    Remove-Item -LiteralPath $voicePidFile -ErrorAction SilentlyContinue
                    Write-Output 'Voice: 没有正在运行的进程（已清理过期的 data/voice.pid）。'
                } elseif (-not $voiceModel) {
                    Write-Output 'Voice: 缺少 config/voice.local.json 或其中的 model 字段，无法验证语音进程，保留进程不动。'
                } elseif (([string]$voiceProcess.Name) -ne 'breeze-server.exe') {
                    Write-Output "Voice: data/voice.pid 指向的进程名是 $($voiceProcess.Name)，不是 breeze-server，保留进程不动。"
                } elseif (-not (([string]$voiceProcess.CommandLine).Replace('/', '\').ToLowerInvariant().Contains($voiceModel.Replace('/', '\').ToLowerInvariant()))) {
                    Write-Output 'Voice: 语音进程命令行不包含配置的模型路径，保留进程不动。'
                } else {
                    Stop-Process -Id $voicePid -ErrorAction SilentlyContinue
                    $stopping = Get-Process -Id $voicePid -ErrorAction SilentlyContinue
                    if ($stopping -and -not $stopping.WaitForExit(10000)) {
                        Stop-Process -Id $voicePid -Force -ErrorAction SilentlyContinue
                        Start-Sleep -Milliseconds 500
                    }
                    if (Get-Process -Id $voicePid -ErrorAction SilentlyContinue) {
                        Write-Output "Voice: 无法停止语音进程（pid $voicePid），请手动检查。"
                    } else {
                        Remove-Item -LiteralPath $voicePidFile -ErrorAction SilentlyContinue
                        Write-Output "Voice stopped (pid $voicePid)."
                    }
                }
            }
        }
    } catch { Write-Output "Voice: 停止语音进程时出错：$($_.Exception.Message)" }
} else {
    Write-Output 'Voice: 保留运行中的语音进程（-KeepVoice）。'
}
if (-not $KeepVoice) {
    foreach ($memoryScript in @('stop-memory-worker.ps1','stop-memory-model.ps1')) {
        if (Test-Path -LiteralPath (Join-Path $PSScriptRoot $memoryScript)) {
            try { & (Join-Path $PSScriptRoot $memoryScript) } catch { Write-Output "Memory shutdown failed: $($_.Exception.Message)" }
        }
    }
}
if (-not $KeepVoice -and (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'stop-https.ps1'))) {
    try { & (Join-Path $PSScriptRoot 'stop-https.ps1') } catch { Write-Output "HTTPS shutdown failed: $($_.Exception.Message)" }
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
