[CmdletBinding()]
param(
    [ValidateRange(1, 600)]
    [int]$PollIntervalSeconds = 60,

    [ValidateRange(60, 86400)]
    [int]$OverallTimeoutSeconds = 86400,

    [ValidateRange(60, 86400)]
    [int]$ChildTimeoutSeconds = 21600,

    [ValidateScript({ $_ -eq -1 -or $_ -ge 1 })]
    [int]$NJobs = 4
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$artifactDir = Join-Path $repoRoot 'simul_log_smoke\beta_zero_recovery_26092_26093'
$contract = Join-Path $artifactDir 'foundation_pipeline_contract_v3.json'
$outputDir = Join-Path $artifactDir 'foundation_stage1_model_family_confirmation_v1'
$pidFile = Join-Path $artifactDir '.foundation_stage1_model_family_confirmation_v1.pid.json'
$stdout = Join-Path $artifactDir 'foundation_stage1_model_family_confirmation_v1.stdout.log'
$stderr = Join-Path $artifactDir 'foundation_stage1_model_family_confirmation_v1.stderr.log'
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
$watcher = Join-Path $repoRoot 'watch_ipmsm_v2_model_family_confirmation.py'

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Required project Python environment is unavailable: $python"
}
if (-not (Test-Path -LiteralPath $contract -PathType Leaf)) {
    throw "Sealed pipeline contract is unavailable: $contract"
}
if (-not (Test-Path -LiteralPath $watcher -PathType Leaf)) {
    throw "Model-family confirmation watcher is unavailable: $watcher"
}
if ($OverallTimeoutSeconds -lt $PollIntervalSeconds) {
    throw 'OverallTimeoutSeconds must be greater than or equal to PollIntervalSeconds.'
}

$arguments = @(
    $watcher,
    '--contract', $contract,
    '--output-dir', $outputDir,
    '--pid-file', $pidFile,
    '--execute',
    '--poll-interval-seconds', $PollIntervalSeconds,
    '--overall-timeout-seconds', $OverallTimeoutSeconds,
    '--child-timeout-seconds', $ChildTimeoutSeconds,
    '--n-jobs', $NJobs
)
if ((Test-Path -LiteralPath $outputDir) -or (Test-Path -LiteralPath $pidFile)) {
    $arguments += '--resume'
}

$exitCode = 1
Push-Location -LiteralPath $repoRoot
try {
    & $python @arguments 1>> $stdout 2>> $stderr
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $exitCode
