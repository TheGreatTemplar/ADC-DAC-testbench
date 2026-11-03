$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
$pyCommand = Get-Command py -ErrorAction SilentlyContinue
$bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$bootstrapPython = $null
$bootstrapArgs = @()
if ($pyCommand)
{
    $bootstrapPython = $pyCommand.Source
    $bootstrapArgs = @('-3')
}
elseif ($pythonCommand)
{
    $bootstrapPython = $pythonCommand.Source
}
elseif (Test-Path -LiteralPath $bundledPython)
{
    $bootstrapPython = $bundledPython
}
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe'))
{
    if ($pyCommand)
    {
        & $pyCommand.Source -3 -m venv --without-pip .venv
    }
    elseif ($pythonCommand)
    {
        & $pythonCommand.Source -m venv --without-pip .venv
    }
    elseif (Test-Path -LiteralPath $bundledPython)
    {
        & $bundledPython -m venv --without-pip .venv
    }
    else
    {
        throw 'Install Python 3.12 or later, then run setup.ps1 again.'
    }
    if ($LASTEXITCODE -ne 0)
    {
        throw 'Failed to create Python environment.'
    }
}
& '.\.venv\Scripts\python.exe' -c "import sys; from importlib.util import find_spec; sys.exit(0 if find_spec('pip') else 1)"
if ($LASTEXITCODE -ne 0)
{
    if (-not $bootstrapPython)
    {
        throw 'Python is required to prepare pip in the local environment.'
    }
    & $bootstrapPython @bootstrapArgs -m pip --isolated --python '.venv\Scripts\python.exe' install --index-url https://pypi.org/simple pip
    if ($LASTEXITCODE -ne 0)
    {
        throw 'Failed to prepare pip.'
    }
}
& '.\.venv\Scripts\python.exe' -m pip --isolated install --index-url https://pypi.org/simple -r requirements.txt
if ($LASTEXITCODE -ne 0)
{
    throw 'Failed to install dependencies.'
}
