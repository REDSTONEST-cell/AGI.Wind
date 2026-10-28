# Run once in an elevated PowerShell. This does not start any service.
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run this script in an administrator PowerShell.' }
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$config = Get-Content -LiteralPath (Join-Path $projectRoot 'config\https.local.json') -Raw | ConvertFrom-Json
$bindAddress = [Net.IPAddress]::Parse([string]$config.bind_address).ToString()
$dnsEnabled = $config.dns_provider -eq 'alidns'
$exeName = if ($dnsEnabled) { 'caddy-alidns.exe' } else { 'caddy.exe' }
$exe = Join-Path $projectRoot ".devtools\caddy\$exeName"
$httpsPort = if ($config.https_port) { [int]$config.https_port } else { 443 }
if ($httpsPort -lt 1 -or $httpsPort -gt 65535) { throw 'Invalid HTTPS port.' }
$ports = if ($dnsEnabled) { @($httpsPort) } else { @(80,$httpsPort) }
if (-not (Test-Path -LiteralPath $exe)) { throw 'Caddy executable is missing.' }
$ruleName = 'Nagi-HTTPS-IPv6-Caddy'
$existingRule = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
if ($existingRule) {
    $oldProgram = ($existingRule | Get-NetFirewallApplicationFilter).Program
    $allowedExecutables = @((Join-Path $projectRoot '.devtools\caddy\caddy.exe'), (Join-Path $projectRoot '.devtools\caddy\caddy-alidns.exe'))
    if ($oldProgram -notin $allowedExecutables) { throw 'Existing rule belongs to another program; refusing to overwrite it.' }
    Set-NetFirewallRule -Name $ruleName -NewDisplayName 'Nagi HTTPS IPv6 (Caddy)' -Enabled True -Direction Inbound -Action Allow -Protocol TCP -LocalPort $ports -LocalAddress $bindAddress -Program $exe -Profile Any | Out-Null
} else {
    New-NetFirewallRule -Name $ruleName -DisplayName 'Nagi HTTPS IPv6 (Caddy)' -Direction Inbound -Action Allow -Protocol TCP -LocalPort $ports -LocalAddress $bindAddress -Program $exe -Profile Any | Out-Null
}
Write-Output "Configured narrow inbound rule for Caddy TCP $($ports -join ',') at the configured IPv6 address."
Write-Output "To undo: Remove-NetFirewallRule -Name '$ruleName'"
