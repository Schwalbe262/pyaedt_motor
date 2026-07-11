[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8765
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$artifactDir = Join-Path $repoRoot 'simul_log_smoke\beta_zero_recovery_26092_26093'
# This dashboard is stdlib-only.  Prefer the base interpreter because the
# Microsoft Store venv launcher can detach from Task Scheduler on Windows.
$python = (Get-Command python.exe -ErrorAction Stop).Source

$stdout = Join-Path $artifactDir 'foundation_dashboard.stdout.log'
$stderr = Join-Path $artifactDir 'foundation_dashboard.stderr.log'
$process = Start-Process `
    -FilePath $python `
    -ArgumentList @('ipmsm_dashboard.py', '--host', '127.0.0.1', '--port', $Port) `
    -WorkingDirectory $repoRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -PassThru
$process.WaitForExit()
exit $process.ExitCode
