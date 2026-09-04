$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendDir = Join-Path $projectRoot "backend"
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"

Set-Location $backendDir
& $pythonExe -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
