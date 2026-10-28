param([switch]$Staging, [switch]$ValidateOnly)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$configPath = Join-Path $projectRoot 'config\https.local.json'
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$dnsEnabled = $config.dns_provider -eq 'alidns'
$exeName = if ($dnsEnabled) { 'caddy-alidns.exe' } else { 'caddy.exe' }
$exe = Join-Path $projectRoot ".devtools\caddy\$exeName"
if (-not (Test-Path -LiteralPath $exe)) { throw "Caddy is missing: $exeName" }
$httpsPort = if ($config.https_port) { [int]$config.https_port } else { 443 }
if ($httpsPort -lt 1 -or $httpsPort -gt 65535) { throw 'Invalid HTTPS port.' }
$siteName = [string]$config.hostname
if ($siteName -notmatch '^[a-z0-9]+(?:[.-][a-z0-9]+)*\.[a-z]{2,}$') { throw 'Invalid HTTPS hostname.' }
$bindAddress = [Net.IPAddress]::Parse([string]$config.bind_address)
if ($bindAddress.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetworkV6 -or $bindAddress.IsIPv6LinkLocal -or [Net.IPAddress]::IsLoopback($bindAddress)) { throw 'A public IPv6 bind address is required.' }
$localAddress = Get-NetIPAddress -AddressFamily IPv6 | Where-Object IPAddress -eq $bindAddress.ToString()
if (-not $localAddress) { throw 'Configured IPv6 address is no longer assigned to this computer. Update config/https.local.json and the hostname.' }
$gatewayPort = [int]$config.gateway_port
if ($gatewayPort -lt 1 -or $gatewayPort -gt 65535) { throw 'Invalid Gateway port.' }
$upstreamHost = if ($config.upstream_host) { [string]$config.upstream_host } else { '127.0.0.1' }
if ($upstreamHost -notmatch '^[A-Za-z0-9.:-]+$') { throw 'Invalid upstream host.' }
$stateDir = Join-Path $projectRoot 'data\https'
[IO.Directory]::CreateDirectory($stateDir) | Out-Null
$suffix = if ($Staging) { 'staging' } else { 'production' }
$storage = (Join-Path $stateDir "caddy-$suffix").Replace('\','/')
$issuer = if ($Staging) { 'https://acme-staging-v02.api.letsencrypt.org/directory' } else { 'https://acme-v02.api.letsencrypt.org/directory' }
$caddyFile = Join-Path $stateDir "Caddyfile.$suffix"
$pidFile = Join-Path $stateDir "caddy-$suffix.pid"
$dnsOptions = ''
$httpRedirect = @"
http://$siteName {
    redir https://{host}{uri} permanent
}
"@
if ($dnsEnabled) {
    $dnsOptions = @'
    acme_dns alidns {
        access_key_id {env.ALIYUN_ACCESS_KEY_ID}
        access_key_secret {env.ALIYUN_ACCESS_KEY_SECRET}
    }
'@
    $httpRedirect = ''
}
$contents = @"
{
    admin off
    persist_config off
    auto_https disable_redirects
    default_bind $bindAddress
    storage file_system {
        root "$storage"
    }
    acme_ca $issuer
$dnsOptions
    servers {
        protocols h1 h2
    }
}
https://${siteName}:$httpsPort {
    reverse_proxy ${upstreamHost}:$gatewayPort {
        flush_interval -1
    }
}
$httpRedirect
"@
[IO.File]::WriteAllText($caddyFile, $contents, (New-Object Text.UTF8Encoding($false)))
$oldKeyId = $env:ALIYUN_ACCESS_KEY_ID
$oldKeySecret = $env:ALIYUN_ACCESS_KEY_SECRET
try {
if ($dnsEnabled) {
    Add-Type -AssemblyName System.Security
    $encrypted = [IO.File]::ReadAllBytes((Join-Path $stateDir 'alidns.dpapi'))
    $decoded = [Security.Cryptography.ProtectedData]::Unprotect($encrypted, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
    $credentials = [Text.Encoding]::UTF8.GetString($decoded) | ConvertFrom-Json
    $env:ALIYUN_ACCESS_KEY_ID = [string]$credentials.id
    $env:ALIYUN_ACCESS_KEY_SECRET = [string]$credentials.secret
    [Array]::Clear($decoded, 0, $decoded.Length)
    $credentials = $null
}
& $exe validate --config $caddyFile --adapter caddyfile
if ($LASTEXITCODE -ne 0) { throw 'Caddy configuration validation failed.' }
if ($ValidateOnly) { Write-Output "Configuration valid ($suffix); no listener started."; return }
if (Test-Path -LiteralPath $pidFile) {
    $savedPid = [int](Get-Content -LiteralPath $pidFile -Raw)
    $existing = Get-CimInstance Win32_Process -Filter "ProcessId=$savedPid" -ErrorAction SilentlyContinue
    if ($existing -and $existing.ExecutablePath -eq $exe -and $existing.CommandLine.Contains($caddyFile)) {
        Write-Output "Caddy already running ($suffix), PID $savedPid."
        return
    }
}
# IPv4 listeners belonging to other applications do not belong to this deployment.
$listenPorts = if ($dnsEnabled) { @($httpsPort) } else { @(80,$httpsPort) }
$busy = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in $listenPorts -and $_.LocalAddress -in @('::', $bindAddress.ToString()) }
if ($busy) { throw 'An IPv6 listener already owns the HTTPS port. Do not stop an unrelated application.' }
$arguments = @('run','--config',('"' + $caddyFile + '"'),'--adapter','caddyfile')
$process = Start-Process -FilePath $exe -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $stateDir "$suffix.stdout.log") -RedirectStandardError (Join-Path $stateDir "$suffix.stderr.log") -PassThru
[IO.File]::WriteAllText($pidFile, [string]$process.Id)
Start-Sleep -Seconds 2
if ($process.HasExited) { throw "Caddy exited. Read data/https/$suffix.stderr.log." }
Write-Output "Caddy started ($suffix), PID $($process.Id). Certificate issuance may still be pending."
Write-Output "HTTPS URL: https://${siteName}:$httpsPort/"
if ($Staging) { Write-Output 'STAGING certificates are NOT browser-trusted. Use only to verify ACME connectivity.' }
Write-Output "Read data/https/$suffix.stderr.log for certificate status; verify from an external network."
} finally {
    $env:ALIYUN_ACCESS_KEY_ID = $oldKeyId
    $env:ALIYUN_ACCESS_KEY_SECRET = $oldKeySecret
}
