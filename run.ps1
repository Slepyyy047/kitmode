param([ValidateSet('check','demo','run')][string]$Mode = 'check')
$ErrorActionPreference = 'Stop'
$kitmodeCandidates = @((Join-Path $PSScriptRoot '.venv\Scripts\python.exe'))
$kitmodeCommand = Get-Command python -ErrorAction SilentlyContinue
if ($kitmodeCommand -and $kitmodeCommand.Source -notlike '*WindowsApps*') {
    $kitmodeCandidates += $kitmodeCommand.Source
}
$kitmodeCandidates += (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')
$kitmodePython = $null
foreach ($kitmodeCandidate in $kitmodeCandidates) {
    if (Test-Path -LiteralPath $kitmodeCandidate) {
        & $kitmodeCandidate -c "import sys; from zoneinfo import ZoneInfo; assert sys.version_info >= (3,12); ZoneInfo('Europe/Kyiv')" 2>$null
        if ($LASTEXITCODE -eq 0) { $kitmodePython = $kitmodeCandidate; break }
    }
}
if (-not $kitmodePython) {
    throw 'Install Python 3.12+, then pip install -e . in this folder. See docs/QUICKSTART.uk.md.'
}
Push-Location -LiteralPath $PSScriptRoot
try { & $kitmodePython -m kitmode $Mode; $kitmodeExit = $LASTEXITCODE }
finally { Pop-Location }
exit $kitmodeExit
