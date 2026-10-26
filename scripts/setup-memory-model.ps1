param()
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pythonExe = Join-Path $projectRoot '.devtools\memory-venv\Scripts\python.exe'
$basePython = Join-Path $projectRoot 'app\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    if (-not (Test-Path -LiteralPath $basePython)) { throw 'Set up app/.venv first (Python 3.12).' }
    & $basePython -m venv (Join-Path $projectRoot '.devtools\memory-venv')
    if ($LASTEXITCODE -ne 0) { throw 'Memory environment creation failed.' }
}
$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
$uvExe = if ($uvCommand) { $uvCommand.Source } else { Join-Path $env:USERPROFILE '.local\bin\uv.exe' }
if (-not (Test-Path -LiteralPath $uvExe)) { throw 'uv is required for the isolated, hash-locked CPU environment.' }
& $uvExe pip sync --python $pythonExe --require-hashes (Join-Path $PSScriptRoot 'requirements-memory.lock')
if ($LASTEXITCODE -ne 0) { throw 'Memory dependency installation failed.' }
& $pythonExe (Join-Path $PSScriptRoot 'memory-server.py') --root $projectRoot --setup
if ($LASTEXITCODE -ne 0) { throw 'Memory model setup failed; no service was started.' }
Write-Output '[Memory] CPU model installed. Run scripts/start-memory-model.ps1 to start port 8141.'
