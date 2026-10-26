param()
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'http-local.ps1')
$stateDir = Join-Path $projectRoot 'data'
$configPath = Join-Path $projectRoot 'config\asr.local.json'
$pythonExe = Join-Path $projectRoot '.devtools\asr-venv\Scripts\python.exe'
$serverScript = Join-Path $PSScriptRoot 'asr_server.py'
$pidFile = Join-Path $stateDir 'asr.pid'
if (-not (Test-Path -LiteralPath $configPath) -or -not (Test-Path -LiteralPath $pythonExe)) {
    Write-Output '[ASR] 尚未安装，运行 scripts/setup-asr.ps1 后可启用实时通话。'
    return
}
if (Test-Path -LiteralPath $pidFile) {
    $savedAsrId = [int](Get-Content -LiteralPath $pidFile -Raw)
    $existing = Get-CimInstance Win32_Process -Filter "ProcessId=$savedAsrId" -ErrorAction SilentlyContinue
    if ($existing -and $existing.CommandLine -and $existing.CommandLine.Contains($serverScript) -and $existing.CommandLine.Contains($projectRoot)) {
        try {
            $reply = Invoke-LocalHttp -Uri 'http://127.0.0.1:8139/health' -TimeoutMs 2000
            if ($reply.StatusCode -eq 200) { Write-Output '[ASR] 实时识别服务已运行，端口 8139。'; return }
        } catch {}
        throw '已有项目 ASR 进程但尚未就绪，请检查 data/asr.stderr.log。'
    }
}
if (Get-NetTCPConnection -State Listen -LocalPort 8139 -ErrorAction SilentlyContinue) { throw '8139 已被其他服务占用，未启动或接管该进程。' }
$tokenPath = Join-Path $stateDir 'asr-token.txt'
if (-not (Test-Path -LiteralPath $tokenPath)) {
    & $pythonExe -c "import secrets,pathlib; pathlib.Path(r'$tokenPath').write_text(secrets.token_urlsafe(32), encoding='utf-8')"
    if ($LASTEXITCODE -ne 0) { throw 'ASR 私有凭据创建失败。' }
}
$process = Start-Process -FilePath $pythonExe -ArgumentList @('-u',('"'+$serverScript+'"'),'--root',('"'+$projectRoot+'"')) -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $stateDir 'asr.stdout.log') -RedirectStandardError (Join-Path $stateDir 'asr.stderr.log') -PassThru
[IO.File]::WriteAllText($pidFile, "$($process.Id)")
for ($attempt=0; $attempt -lt 60; $attempt++) {
    Start-Sleep -Milliseconds 250
    if ($process.HasExited) { throw 'ASR 启动失败，请检查 data/asr.stderr.log。' }
    try {
        $reply = Invoke-LocalHttp -Uri 'http://127.0.0.1:8139/health' -TimeoutMs 1000
        if ($reply.StatusCode -eq 200) { Write-Output '[ASR] 实时识别服务已就绪：http://127.0.0.1:8139'; return }
    } catch {}
}
throw 'ASR 启动尚未完成，请查看 data/asr.stderr.log。'
