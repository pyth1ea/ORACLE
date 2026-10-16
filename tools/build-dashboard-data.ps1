# Kept for compatibility: the build now lives in tools/build.py.
$py = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
& $py (Join-Path $PSScriptRoot 'build.py')
exit $LASTEXITCODE
