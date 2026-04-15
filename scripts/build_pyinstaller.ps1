$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root "WatchDogEenv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "找不到虛擬環境 Python: $python"
}

Push-Location $root
try {
    & $python -m pip install pyinstaller
    & $python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name WatchDog `
        --paths src `
        src\watchdog_app\main.py
}
finally {
    Pop-Location
}
