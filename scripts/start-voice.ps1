# Start the optional local Breeze-TTS voice service without opening a console window.
# This script deliberately fails soft: a missing voice configuration, missing model paths or a
# slow voice service only print a warning so scripts/start-local.ps1 can still start the gateway.
param([string]$ConfigPath = '')
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$scriptDir = $PSScriptRoot
. (Join-Path $scriptDir 'http-local.ps1')
if (-not $ConfigPath) { $ConfigPath = Join-Path $projectRoot 'config\voice.local.json' }
$stateDir = Join-Path $projectRoot 'data'
$pidFile = Join-Path $stateDir 'voice.pid'
$stdoutLog = Join-Path $stateDir 'voice.stdout.log'
$stderrLog = Join-Path $stateDir 'voice.stderr.log'
$healthTimeoutSeconds = 180

function Write-VoiceWarning([string]$Message) {
    Write-Output ('[语音] ' + $Message)
}

function Resolve-VoicePath([string]$Value) {
    if (-not $Value) { return $null }
    if ([System.IO.Path]::IsPathRooted($Value)) { return $Value }
    return (Join-Path $projectRoot $Value)
}

function Test-VoiceHealth([int]$VoicePort, [int]$TimeoutMs = 2000) {
    try {
        $reply = Invoke-LocalHttp -Uri "http://127.0.0.1:$VoicePort/health" -TimeoutMs $TimeoutMs
        return ($reply.StatusCode -eq 200)
    } catch { return $false }
}

function Get-VoiceId([int]$VoicePort) {
    try {
        $reply = Invoke-LocalHttp -Uri "http://127.0.0.1:$VoicePort/v1/voices" -TimeoutMs 3000
        if ($reply.StatusCode -ne 200) { return $null }
        $payload = $reply.Body | ConvertFrom-Json
        if ($null -eq $payload) { return $null }
        if ($payload -is [System.Array]) { $rows = @($payload) }
        elseif ($null -ne $payload.voices) { $rows = @($payload.voices) }
        elseif ($null -ne $payload.id) { $rows = @($payload) }
        else { $rows = @() }
        foreach ($row in $rows) {
            if ($null -ne $row.id -and [string]$row.id) { return [string]$row.id }
        }
    } catch { return $null }
    return $null
}

function Test-CommandLineArgument([string]$CommandLine, [string]$Name, [string]$Value) {
    if (-not $CommandLine) { return $false }
    $normalized = ' ' + $CommandLine.Replace('/', '\').Replace('"', '').ToLowerInvariant() + ' '
    return ($normalized.Contains(" $Name $Value ") -or $normalized.Contains(" $Name=$Value "))
}

function Test-VoiceProcessModel($VoiceProcess, [string]$ModelPath) {
    if ($null -eq $VoiceProcess -or -not $ModelPath) { return $false }
    if (([string]$VoiceProcess.Name) -ne 'breeze-server.exe') { return $false }
    $commandLine = [string]$VoiceProcess.CommandLine
    if (-not $commandLine) { return $false }
    $needle = $ModelPath.Replace('/', '\').ToLowerInvariant()
    return $commandLine.Replace('/', '\').ToLowerInvariant().Contains($needle)
}

function Find-VoiceProcess([string]$ModelPath) {
    try {
        $candidates = Get-CimInstance Win32_Process -Filter "Name='breeze-server.exe'" -ErrorAction SilentlyContinue
    } catch { return $null }
    foreach ($candidate in @($candidates)) {
        if (Test-VoiceProcessModel -VoiceProcess $candidate -ModelPath $ModelPath) { return $candidate }
    }
    return $null
}

function Get-ManagedVoiceProcess {
    if (-not (Test-Path -LiteralPath $pidFile)) { return $null }
    $raw = Get-Content -LiteralPath $pidFile -Raw -ErrorAction SilentlyContinue
    if ($null -eq $raw) { return $null }
    $savedPid = 0
    if (-not [int]::TryParse($raw.Trim(), [ref]$savedPid)) { return $null }
    if ($savedPid -le 0) { return $null }
    try {
        return (Get-CimInstance Win32_Process -Filter "ProcessId=$savedPid" -ErrorAction SilentlyContinue)
    } catch { return $null }
}

function Save-VoicePid($VoiceProcess) {
    if (-not (Test-Path -LiteralPath $stateDir)) { New-Item -ItemType Directory -Force -Path $stateDir | Out-Null }
    [IO.File]::WriteAllText($pidFile, [string]$VoiceProcess.ProcessId)
}

function Wait-VoiceHealth([int]$VoicePort) {
    $deadline = [DateTime]::UtcNow.AddSeconds($healthTimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 1000
        if (Test-VoiceHealth -VoicePort $VoicePort) { return $true }
    }
    return $false
}

function Write-VoiceRunning([int]$VoicePort, [bool]$Reused) {
    if ($Reused) {
        Write-Output "Voice running: http://127.0.0.1:$VoicePort (reused existing process)"
    } else {
        Write-Output "Voice running: http://127.0.0.1:$VoicePort"
    }
    $voiceId = Get-VoiceId -VoicePort $VoicePort
    if ($voiceId) { Write-Output "Voice id: $voiceId" }
}

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    Write-VoiceWarning "未找到语音配置 $ConfigPath；跳过本地语音启动，网关继续运行。可复制 config/voice.example.json 为 config/voice.local.json 并按本机路径修改。"
    exit 0
}
try {
    $voiceConfig = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
} catch {
    Write-VoiceWarning "语音配置不是有效的 JSON：$ConfigPath；跳过本地语音启动，网关继续运行。"
    exit 0
}
if ($null -eq $voiceConfig) {
    Write-VoiceWarning "语音配置为空：$ConfigPath；跳过本地语音启动，网关继续运行。"
    exit 0
}
$model = Resolve-VoicePath ([string]$voiceConfig.model)
if (-not $model) {
    Write-VoiceWarning "语音配置缺少 model 字段：$ConfigPath；跳过本地语音启动，网关继续运行。"
    exit 0
}
$bindHost = '127.0.0.1'
if ($null -ne $voiceConfig.host -and ([string]$voiceConfig.host).Trim()) { $bindHost = ([string]$voiceConfig.host).Trim() }
$port = 8137
if ($null -ne $voiceConfig.port -and ([string]$voiceConfig.port).Trim()) { $port = [int]$voiceConfig.port }
if ($port -lt 1 -or $port -gt 65535) {
    Write-VoiceWarning "语音配置的 port 无效：$($voiceConfig.port)；跳过本地语音启动，网关继续运行。"
    exit 0
}
$chunkFirst = 4
if ($null -ne $voiceConfig.chunk_first -and ([string]$voiceConfig.chunk_first).Trim()) { $chunkFirst = [int]$voiceConfig.chunk_first }
$chunkMax = 40
if ($null -ne $voiceConfig.chunk_max -and ([string]$voiceConfig.chunk_max).Trim()) { $chunkMax = [int]$voiceConfig.chunk_max }
$webui = $true
if ($null -ne $voiceConfig.webui) { $webui = [bool]$voiceConfig.webui }

# A healthy endpoint is reused; the matching process is identified by its command line so we
# never record a pid from an unrelated service listening on the same port.
if (Test-VoiceHealth -VoicePort $port) {
    $runningProcess = Find-VoiceProcess -ModelPath $model
    if ($null -ne $runningProcess) {
        Save-VoicePid $runningProcess
        Write-VoiceRunning -VoicePort $port -Reused $true
        exit 0
    }
    Write-VoiceWarning "http://127.0.0.1:$port/health 有响应，但没有找到命令行包含模型路径的 breeze-server 进程；未写入 data/voice.pid。"
    exit 0
}

# A previous start may still be loading the model (Breeze can take a while on first use).
$managedProcess = Get-ManagedVoiceProcess
if (Test-VoiceProcessModel -VoiceProcess $managedProcess -ModelPath $model) {
    if (-not (Test-CommandLineArgument ([string]$managedProcess.CommandLine) '--port' ([string]$port))) {
        Write-VoiceWarning "data/voice.pid 管理的语音进程使用其它端口，已跳过自动启动以避免重复实例。需要切换端口时请先运行 scripts/stop-local.ps1。"
        exit 0
    }
    Write-Output "Voice process from data/voice.pid is running; waiting for http://127.0.0.1:$port/health ..."
    if (Wait-VoiceHealth -VoicePort $port) {
        Write-VoiceRunning -VoicePort $port -Reused $true
        exit 0
    }
    Write-VoiceWarning "语音进程在 $healthTimeoutSeconds 秒内仍未就绪；请查看 data/voice.stdout.log 与 data/voice.stderr.log。网关继续运行。"
    exit 0
}

$binary = Resolve-VoicePath ([string]$voiceConfig.binary)
if (-not $binary) {
    Write-VoiceWarning "语音配置缺少 binary 字段：$ConfigPath；跳过本地语音启动，网关继续运行。"
    exit 0
}
if (-not (Test-Path -LiteralPath $binary -PathType Leaf)) {
    Write-VoiceWarning "未找到 breeze-server.exe：$binary；跳过本地语音启动，网关继续运行。请在 config/voice.local.json 中修正 binary。"
    exit 0
}
$voicesDir = Resolve-VoicePath ([string]$voiceConfig.voices_dir)
if (-not $voicesDir) {
    Write-VoiceWarning "语音配置缺少 voices_dir 字段：$ConfigPath；跳过本地语音启动，网关继续运行。"
    exit 0
}
if (-not (Test-Path -LiteralPath $voicesDir -PathType Container)) {
    Write-VoiceWarning "未找到语音素材目录 voices_dir：$voicesDir；跳过本地语音启动，网关继续运行。"
    exit 0
}
if (-not (Test-Path -LiteralPath $model -PathType Leaf)) {
    Write-VoiceWarning "未找到语音模型：$model；跳过本地语音启动，网关继续运行。请在 config/voice.local.json 中修正 model。"
    exit 0
}
if (-not (Test-Path -LiteralPath $stateDir)) { New-Item -ItemType Directory -Force -Path $stateDir | Out-Null }

$argumentList = @(('"' + $model + '"'), '--host', $bindHost, '--port', ([string]$port))
if ($webui) { $argumentList += '--webui' }
$argumentList += @('--voices-dir', ('"' + $voicesDir + '"'), '--chunk-first', ([string]$chunkFirst), '--chunk-max', ([string]$chunkMax))
try {
    $voiceProcess = Start-Process -FilePath $binary -ArgumentList $argumentList -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -PassThru
} catch {
    Write-VoiceWarning "无法启动 breeze-server：$($_.Exception.Message)；网关继续运行。"
    exit 0
}
[IO.File]::WriteAllText($pidFile, [string]$voiceProcess.Id)

$healthy = $false
$deadline = [DateTime]::UtcNow.AddSeconds($healthTimeoutSeconds)
while ([DateTime]::UtcNow -lt $deadline) {
    Start-Sleep -Milliseconds 1000
    if ($voiceProcess.HasExited) { break }
    if (Test-VoiceHealth -VoicePort $port) { $healthy = $true; break }
}
if (-not $healthy) {
    if ($voiceProcess.HasExited) {
        Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue
        Write-VoiceWarning "breeze-server 已退出（ExitCode=$($voiceProcess.ExitCode)）；请查看 data/voice.stderr.log。网关继续运行。"
    } else {
        Write-VoiceWarning "语音服务在 $healthTimeoutSeconds 秒内未就绪，进程仍在运行；请查看 data/voice.stdout.log 与 data/voice.stderr.log。网关继续运行。"
    }
    exit 0
}
Write-VoiceRunning -VoicePort $port -Reused $false
exit 0
