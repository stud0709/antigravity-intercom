param(
    [ValidateSet('start', 'status', 'stop')]
    [string]$Action = 'start'
)

# Foreground by design: the user owns this terminal and the broker lifecycle.
$intercomPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$intercomBroker = Join-Path $PSScriptRoot '.agents\skills\antigravity-intercom\broker.py'
if (-not (Test-Path -LiteralPath $intercomPython -PathType Leaf)) {
    Write-Error 'Create .venv and install requirements.txt first; see the setup guide.'
    exit 1
}
& $intercomPython $intercomBroker $Action
exit $LASTEXITCODE
