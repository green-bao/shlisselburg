$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Python проекта не найден. Откройте README.md для установки зависимостей.'
}
& $pythonPath (Join-Path $PSScriptRoot 'app.py')
