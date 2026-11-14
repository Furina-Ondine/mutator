#requires -Version 7.3
# The caller's working directory is the project to analyze.
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$PSNativeCommandArgumentPassing = 'Standard'

$environmentRoot = Join-Path $PSScriptRoot '.venv'
$pythonPath = if ($IsWindows) { 'Scripts/python.exe' } else { 'bin/python' }
$entryPath = if ($IsWindows) { 'Scripts/mutator.exe' } else { 'bin/mutator' }
$toolPython = Join-Path $environmentRoot $pythonPath
$entryPoint = Join-Path $environmentRoot $entryPath

if (-not (Test-Path -LiteralPath $toolPython -PathType Leaf)) {
    if ($IsWindows -and (Get-Command py -CommandType Application -ErrorAction SilentlyContinue)) {
        & py -3 -m venv $environmentRoot
    } else {
        $bootstrapPython = if ($IsWindows) { 'python' } else { 'python3' }
        & $bootstrapPython -m venv $environmentRoot
    }
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

if (-not (Test-Path -LiteralPath $entryPoint -PathType Leaf)) {
    & $toolPython -m pip install -e $PSScriptRoot |
        ForEach-Object { [Console]::Error.WriteLine($_) }
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

& $toolPython -m mutator @args
exit $LASTEXITCODE
