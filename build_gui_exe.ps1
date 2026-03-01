$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoRoot

python -m pip install --upgrade pyinstaller

python -m PyInstaller --noconfirm --clean --onefile --windowed --collect-all phoenix6 --name HootMergerGUI hoot_merger_gui.py
if ($LASTEXITCODE -ne 0) {
	throw "PyInstaller build failed. Close any running HootMergerGUI.exe and try again."
}

Write-Host "Build complete: dist/HootMergerGUI.exe"
