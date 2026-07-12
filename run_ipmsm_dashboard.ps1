[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8765
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$artifactDir = Join-Path $repoRoot 'simul_log_smoke\beta_zero_recovery_26092_26093'
$v4StateDir = Join-Path $repoRoot 'simul_log_smoke\v4r3'
$contract = Join-Path $v4StateDir 'base_v3.json'
$v4Contract = Join-Path $v4StateDir 'contract.json'
$runnerLog = Join-Path $artifactDir 'foundation_stage1_runner_supervised.stderr.log'
$targetLoadProgress = Join-Path $artifactDir 'ipmsm_target_load_v4\progress.json'
# This dashboard is stdlib-only.  Prefer the base interpreter because the
# Microsoft Store venv launcher can detach from Task Scheduler on Windows.
$python = (Get-Command python.exe -ErrorAction Stop).Source

$stdout = Join-Path $artifactDir 'foundation_dashboard.stdout.log'
$stderr = Join-Path $artifactDir 'foundation_dashboard.stderr.log'
$arguments = @(
    'ipmsm_dashboard.py',
    '--host', '127.0.0.1',
    '--port', $Port,
    '--contract', $contract,
    '--v4-contract', $v4Contract,
    '--runner-log', $runnerLog,
    '--target-load-progress', $targetLoadProgress
)
$process = Start-Process `
    -FilePath $python `
    -ArgumentList $arguments `
    -WorkingDirectory $repoRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -PassThru
$process.WaitForExit()
exit $process.ExitCode
