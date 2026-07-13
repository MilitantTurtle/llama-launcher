$ErrorActionPreference = 'Stop'
$AppDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = $null

if ($env:LAUNCHPAD_PYTHON) {
    $Python = Get-Item -LiteralPath $env:LAUNCHPAD_PYTHON
} else {
    $Command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($Command) { $Python = Get-Item -LiteralPath $Command.Source }
}

if (-not $Python) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show(
        'Python 3 was not found. Install Python, add it to PATH, or set LAUNCHPAD_PYTHON to python.exe.',
        'Local Model Launchpad'
    ) | Out-Null
    exit 1
}

if (-not (Test-Path -LiteralPath (Join-Path $AppDirectory 'config.json'))) {
    & $Python.FullName (Join-Path $AppDirectory 'setup.py')
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$Pythonw = Join-Path $Python.DirectoryName 'pythonw.exe'
$AppScript = Join-Path $AppDirectory 'app.py'
$AppArgument = '"{0}"' -f $AppScript
if (Test-Path -LiteralPath $Pythonw) {
    Start-Process -FilePath $Pythonw -ArgumentList $AppArgument -WorkingDirectory $AppDirectory
} else {
    Start-Process -FilePath $Python.FullName -ArgumentList $AppArgument -WorkingDirectory $AppDirectory -WindowStyle Hidden
}
