$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $env:LOCALAPPDATA "Pressay\venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 first."
}

$hadPythonPath = Test-Path Env:PYTHONPATH
$previousPythonPath = $env:PYTHONPATH
$hadQtPlatform = Test-Path Env:QT_QPA_PLATFORM
$previousQtPlatform = $env:QT_QPA_PLATFORM
$script = @'
from PySide6.QtWidgets import QApplication
from pressay.ui import MicrophoneChoice, SettingsWindow, StatusOverlay, TrayController, UiSignals

app = QApplication([])
signals = UiSignals()
window = SettingsWindow(
    signals,
    {"model": "small", "language": "auto", "auto_insert": True},
    [MicrophoneChoice(None, "Default")],
)
overlay = StatusOverlay()
tray = TrayController(signals, window)
window.show()
overlay.show_status("Слушаю…", "recording")
app.processEvents()
assert window.isVisible()
assert overlay.isVisible()
assert window.current_settings()["model"] == "small"
tray.tray.hide()
window.prepare_to_quit()
window.close()
overlay.close()
app.processEvents()
print("Pressay UI smoke: OK")
'@

$exitCode = 0
try {
    $env:PYTHONPATH = Join-Path $projectRoot "src"
    $env:QT_QPA_PLATFORM = "offscreen"
    $script | & $venvPython -
    $exitCode = $LASTEXITCODE
}
finally {
    if ($hadPythonPath) {
        $env:PYTHONPATH = $previousPythonPath
    }
    else {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    }
    if ($hadQtPlatform) {
        $env:QT_QPA_PLATFORM = $previousQtPlatform
    }
    else {
        Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue
    }
}

if ($exitCode -ne 0) { exit $exitCode }
