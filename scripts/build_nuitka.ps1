$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root "WatchDogEenv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "找不到虛擬環境 Python: $python"
}

Push-Location $root
try {
    $iconPath = Join-Path $root "src\watchdog_app\assets\icons\WatchDog.ico"
    & $python -m pip install nuitka
    & $python -m nuitka `
        --standalone `
        --windows-console-mode=disable `
        --enable-plugin=pyside6 `
        --windows-icon-from-ico=$iconPath `
        --include-data-dir=src\watchdog_app\assets\icons=watchdog_app\assets\icons `
        --output-dir=dist\nuitka `
        --include-package=watchdog_app `
        src\watchdog_app\main.py
}
finally {
    Pop-Location
}
