param([string]$ModelRoot = (Join-Path $env:USERPROFILE 'Models\ASR-Zipformer'))
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$environmentPath = Join-Path $projectRoot '.devtools\asr-venv'
$pythonExe = Join-Path $environmentPath 'Scripts\python.exe'
$uvExe = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
if (-not (Test-Path -LiteralPath $uvExe)) { $uvExe = (Get-Command uv -ErrorAction Stop).Source }
if (-not (Test-Path -LiteralPath $pythonExe)) {
    & $uvExe venv --python (Join-Path $projectRoot 'app\.venv\Scripts\python.exe') $environmentPath
    if ($LASTEXITCODE -ne 0) { throw '无法创建独立 ASR 环境。' }
}
& $uvExe pip install --python $pythonExe -r (Join-Path $PSScriptRoot 'requirements-asr.txt')
if ($LASTEXITCODE -ne 0) { throw 'ASR 依赖安装失败。' }
New-Item -ItemType Directory -Force $ModelRoot | Out-Null
$modelName = 'sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30'
$modelDirectory = Join-Path $ModelRoot $modelName
$releaseBase = 'https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/'
if (-not (Test-Path -LiteralPath (Join-Path $modelDirectory 'encoder.int8.onnx'))) {
    $archivePath = Join-Path $ModelRoot 'model.tar.bz2'
    & curl.exe --ssl-no-revoke -fL --retry 2 ($releaseBase+$modelName+'.tar.bz2') -o $archivePath
    if ($LASTEXITCODE -ne 0) { throw 'ASR 模型下载失败。' }
    & tar -xjf $archivePath -C $ModelRoot
    if ($LASTEXITCODE -ne 0) { throw 'ASR 模型解压失败。' }
}
$vadPath = Join-Path $ModelRoot 'silero_vad.onnx'
if (-not (Test-Path -LiteralPath $vadPath)) {
    & curl.exe --ssl-no-revoke -fL --retry 2 ($releaseBase+'silero_vad.onnx') -o $vadPath
    if ($LASTEXITCODE -ne 0) { throw 'VAD 模型下载失败。' }
}
foreach ($name in @('encoder.int8.onnx','decoder.onnx','joiner.int8.onnx','tokens.txt')) {
    if (-not (Test-Path -LiteralPath (Join-Path $modelDirectory $name))) { throw "缺少 ASR 文件：$name" }
}
$configPath = Join-Path $projectRoot 'config\asr.local.json'
if (-not (Test-Path -LiteralPath $configPath)) {
    $config = @{model_dir=$modelDirectory; vad_model=$vadPath; threads=2; silence_seconds=0.4} | ConvertTo-Json
    [IO.File]::WriteAllText($configPath, $config, [Text.UTF8Encoding]::new($false))
}
& (Join-Path $PSScriptRoot 'start-asr.ps1')
