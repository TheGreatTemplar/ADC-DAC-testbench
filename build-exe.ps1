param
(
    [string]$Python = '.venv-exe\python.exe'
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYINSTALLER_CONFIG_DIR = Join-Path $PSScriptRoot 'build\pyinstaller-cache'
& $Python -c "import struct, sys; assert sys.version_info[:2] == (3, 8) and struct.calcsize('P') == 4, 'Build requires Python 3.8 x86'"
if ($LASTEXITCODE -ne 0) { throw 'Use setup-exe.ps1 to prepare Python 3.8 x86.' }
$binding = & $Python -c "from qt_compat import QtCore; print(QtCore.__name__.split('.')[0])"
if ($LASTEXITCODE -ne 0) { throw 'Cannot load the build environment.' }
$excludedBinding = if ($binding.Trim() -eq 'PySide2') { 'PySide6' } else { 'PySide2' }
$arguments = @(
    '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--windowed',
    '--name', 'ADC-DAC-Testbench', '--distpath', 'dist', '--workpath', 'build\exe',
    '--specpath', 'build', '--hidden-import', 'serial.urlhandler.protocol_loop',
    '--exclude-module', $excludedBinding, '--exclude-module', 'PyQt5', '--exclude-module', 'PyQt6',
    '--exclude-module', 'matplotlib', '--exclude-module', 'scipy', '--exclude-module', 'pandas',
    '--exclude-module', 'IPython', '--exclude-module', 'capstone',
    '--add-data', "$PSScriptRoot\LICENSE;.", 'app.py'
)
& $Python @arguments
if ($LASTEXITCODE -ne 0) { throw 'EXE build failed.' }
$result = Get-Item -LiteralPath 'dist\ADC-DAC-Testbench.exe'
Write-Output "Ready: $($result.FullName) ($([math]::Round($result.Length / 1MB, 1)) MB)"
