[CmdletBinding()]
param(
    [ValidateRange(1, 600)]
    [int]$PollIntervalSeconds = 60,

    [ValidateRange(60, 86400)]
    [int]$OverallTimeoutSeconds = 21600
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$artifactDir = Join-Path $repoRoot 'simul_log_smoke\beta_zero_recovery_26092_26093'
$contract = Join-Path $artifactDir 'foundation_pipeline_contract_v3.json'
$outputDir = Join-Path $artifactDir 'foundation_stage1_provisional60_v1'
$pidFile = Join-Path $artifactDir '.foundation_stage1_provisional60_v1.checkpoint.pid.json'
$stdout = Join-Path $artifactDir 'foundation_stage1_provisional60_watcher.stdout.log'
$stderr = Join-Path $artifactDir 'foundation_stage1_provisional60_watcher.stderr.log'
$python = (Get-Command python.exe -ErrorAction Stop).Source

$arguments = @(
    'watch_ipmsm_v2_provisional_checkpoint.py',
    '--contract', $contract,
    '--output-dir', $outputDir,
    '--pid-file', $pidFile,
    '--execute',
    '--poll-interval-seconds', $PollIntervalSeconds,
    '--overall-timeout-seconds', $OverallTimeoutSeconds
)
if (Test-Path -LiteralPath $outputDir) {
    $arguments += '--resume'
}

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
