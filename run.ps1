param(
    [ValidateSet('synthetic', 'camera', 'auto')][string]$Source = 'synthetic',
    [ValidateSet('fusion', 'naive')][string]$Mode = 'fusion',
    [int]$Port = 8765,
    [switch]$NoBrowser,
    [switch]$Benchmark
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    throw 'Virtual environment missing. See README.md for Python setup.'
}
if ($Benchmark) {
    & $taskPython -m nebula_mvp.benchmark --verify
} else {
    $taskArgs = @('-m', 'nebula_mvp', '--source', $Source, '--mode', $Mode, '--port', "$Port")
    if (-not $NoBrowser) { $taskArgs += '--open' }
    & $taskPython @taskArgs
}
exit $LASTEXITCODE
