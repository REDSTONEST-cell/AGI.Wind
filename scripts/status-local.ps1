$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'http-local.ps1')
$portFile = Join-Path $projectRoot 'data\gateway-port.txt'
$hostFile = Join-Path $projectRoot 'data\gateway-host.txt'
$port = if (Test-Path -LiteralPath $portFile) { [int](Get-Content -LiteralPath $portFile -Raw) } else { 18440 }
$bindHost = if (Test-Path -LiteralPath $hostFile) { (Get-Content -LiteralPath $hostFile -Raw).Trim() } else { '127.0.0.1' }

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

function Write-VoiceStatus {
    $voiceConfigPath = Join-Path $projectRoot 'config\voice.local.json'
    if (-not (Test-Path -LiteralPath $voiceConfigPath)) {
        Write-Output 'Voice: unavailable (config/voice.local.json not found)'
        return
    }
    $voicePort = 8137
    try {
        $voiceConfig = Get-Content -LiteralPath $voiceConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($null -eq $voiceConfig) { throw 'empty voice config' }
        if ($null -ne $voiceConfig.port -and ([string]$voiceConfig.port).Trim()) { $voicePort = [int]$voiceConfig.port }
    } catch {
        Write-Output 'Voice: unavailable (invalid config/voice.local.json)'
        return
    }
    try {
        $voiceHealth = Invoke-LocalHttp -Uri "http://127.0.0.1:$voicePort/health" -TimeoutMs 2000
        if ($voiceHealth.StatusCode -ne 200) {
            Write-Output 'Voice: unavailable'
            return
        }
        Write-Output "Voice: ready (http://127.0.0.1:$voicePort)"
        $voiceId = Get-VoiceId -VoicePort $voicePort
        if ($voiceId) { Write-Output "Voice id: $voiceId" }
    } catch {
        Write-Output 'Voice: unavailable'
    }
}

try {
    $reply = Invoke-LocalHttp -Uri "http://127.0.0.1:$port/health/live"
    $live = $reply.Body | ConvertFrom-Json
    Write-Output "Gateway: $($live.status) on http://127.0.0.1:$port (bind: $bindHost)"
    if ($bindHost -notin @('127.0.0.1', 'localhost', '::1')) {
        $addresses = Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | Select-Object -ExpandProperty IPAddress
        foreach ($address in $addresses) { Write-Output "  LAN client URL: http://${address}:$port" }
        Write-Output '  Client key authentication is required outside loopback; default key is 123456.'
    }
    $ready = Invoke-LocalHttp -Uri "http://127.0.0.1:$port/health/ready"
    Write-Output $ready.Body
    Write-VoiceStatus
} catch { Write-Output 'Gateway is not running.'; Write-VoiceStatus; exit 1 }
