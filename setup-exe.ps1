param
(
    [string]$InstallerPython = '.venv\Scripts\python.exe'
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
New-Item -ItemType Directory -Force -Path downloads, .venv-exe | Out-Null
$archive = Join-Path $PSScriptRoot 'downloads\python-3.8.10-embed-win32.zip'
if (-not (Test-Path -LiteralPath $archive))
{
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.8.10/python-3.8.10-embed-win32.zip' -OutFile $archive
}
Expand-Archive -LiteralPath $archive -DestinationPath .venv-exe -Force
@'
python38.zip
.
..
Lib/site-packages
import site
'@ | Set-Content -LiteralPath .venv-exe\python38._pth -Encoding ASCII
& $InstallerPython -m pip --isolated --disable-pip-version-check --no-input --keyring-provider disabled install --index-url https://pypi.org/simple --only-binary=:all: --platform win32 --python-version 3.8 --implementation cp --abi cp38 --target .venv-exe\Lib\site-packages --upgrade -r requirements-exe.txt
if ($LASTEXITCODE -ne 0) { throw 'EXE build dependencies installation failed.' }
& .\.venv-exe\python.exe -c "from qt_compat import QtCore; import PyInstaller, numpy, serial; print(QtCore.__version__, PyInstaller.__version__)"
if ($LASTEXITCODE -ne 0) { throw 'EXE build environment check failed.' }
Write-Output 'Ready: .venv-exe (Python 3.8 x86 / Qt 5)'
