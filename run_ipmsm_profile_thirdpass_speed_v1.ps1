[CmdletBinding()]
param(
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$artifactDir = Join-Path $repoRoot 'simul_log_smoke\beta_zero_recovery_26092_26093'
$outputDir = Join-Path $repoRoot 'collected\ipmsm_v2_profile_thirdpass_speed_v1'
$analysisDir = Join-Path $repoRoot 'collected\ipmsm_v2_profile_thirdpass_speed_v1_analysis_v1'
$mergedOutput = 'profile_thirdpass_speed_v2s1_paired24_results_v1.csv'
$python = 'C:\Python314\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python interpreter is unavailable: $python"
}

$arguments = @(
    '-u',
    (Join-Path $repoRoot 'run_ipmsm_profile_thirdpass_speed_v1.py'),
    '--cases', (Join-Path $repoRoot 'simul_log_smoke\profile_thirdpass_speed_v2s1_paired24_cases_v1.csv'),
    '--project', 'PYAEDT_MOTOR_IPMSM_V2',
    '--scheduler-url', 'http://127.0.0.1:8000',
    '--project-active-cap', '100',
    '--task-prefix', 'ipmsm-v2-profile-thirdpass-speed-v1',
    '--remote-cases-dir', 'remote/ipmsm_v2_profile_thirdpass_speed_v1',
    '--result-dir', 'simul_log/ipmsm_v2_profile_thirdpass_speed_v1',
    '--simulation-dir', 'simulation/ipmsm_v2_profile_thirdpass_speed_v1',
    '--log-dir', 'simul_log_scheduler/ipmsm_v2_profile_thirdpass_speed_v1_logs',
    '--output-dir', $outputDir,
    '--merged-output', $mergedOutput,
    '--beta-summary', (Join-Path $artifactDir 'beta_mtpa_summary.json'),
    '--beta-case-plan', (Join-Path $artifactDir 'beta_mtpa_cases.csv'),
    '--beta-results', (Join-Path $artifactDir 'beta_mtpa_collected_26094_26103\beta_mtpa_results.csv'),
    '--beta-calibration-manifest', (Join-Path $artifactDir 'beta_zero_manifest.json'),
    '--allowed-quality-profile', 'time_138_p12_baseline',
    '--allowed-quality-profile', 'time_135_p12_iron525',
    '--completed-result-settle-seconds', '300',
    '--timeout', '30'
)
if (-not $DryRun) {
    $arguments += '--submit'
}
$finalizerArguments = @(
    '-u',
    (Join-Path $repoRoot 'finalize_ipmsm_profile_thirdpass_speed_v1.py'),
    '--collection-dir', $outputDir,
    '--output-dir', $analysisDir
)
if (-not $DryRun) {
    $finalizerArguments += '--execute'
}

$logStem = if ($DryRun) {
    'profile_thirdpass_speed_v1.dryrun'
}
else {
    'profile_thirdpass_speed_v1'
}
$stdout = Join-Path $artifactDir "$logStem.stdout.log"
$stderr = Join-Path $artifactDir "$logStem.stderr.log"
$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    if (-not (Test-Path -LiteralPath $outputDir)) {
        & $python @arguments 1>> $stdout 2>> $stderr
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0) {
            exit $exitCode
        }
        if ($DryRun) {
            exit 0
        }
    }
    & $python @finalizerArguments 1>> $stdout 2>> $stderr
    $exitCode = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = $previousErrorActionPreference
}
exit $exitCode
