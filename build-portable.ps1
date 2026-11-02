param
(
    [string]$Python = '.venv\Scripts\python.exe'
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
New-Item -ItemType Directory -Force -Path downloads, portable, portable\runtime | Out-Null
$archive = Join-Path $PSScriptRoot 'downloads\python-3.8.10-embed-win32.zip'
if (-not (Test-Path -LiteralPath $archive))
{
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.8.10/python-3.8.10-embed-win32.zip' -OutFile $archive
}
Expand-Archive -LiteralPath $archive -DestinationPath portable\runtime -Force
@'
python38.zip
.
..
Lib/site-packages
import site
'@ | Set-Content -LiteralPath portable\runtime\python38._pth -Encoding ASCII
& $Python -m pip --isolated install --index-url https://pypi.org/simple --only-binary=:all: --platform win32 --python-version 3.8 --implementation cp --abi cp38 --target portable\runtime\Lib\site-packages --upgrade -r requirements-portable.txt
if ($LASTEXITCODE -ne 0)
{
    throw 'Portable dependencies installation failed.'
}
Copy-Item -LiteralPath app.py, LICENSE, PORTABLE.txt, requirements-portable.txt -Destination portable -Force
Copy-Item -LiteralPath portable-start.cmd -Destination portable\START.cmd -Force
Compress-Archive -Path portable\* -DestinationPath ADC-DAC-testbench-portable.zip -Force
Write-Output 'Ready: ADC-DAC-testbench-portable.zip'
