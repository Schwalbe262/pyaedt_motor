[CmdletBinding()]
param(
    [switch]$PreparePlan,
    [switch]$Submit
)

$ErrorActionPreference = 'Stop'
if ($PreparePlan -and $Submit) {
    throw '-PreparePlan and -Submit must be separate operator steps.'
}

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$sourcePlan = Join-Path $repoRoot 'simul_log_smoke\profile_thirdpass_speed_v2s1_paired24_cases_v1.csv'
$pilotPlan = Join-Path $repoRoot 'simul_log_smoke\profile_affinityfix_replay2_cases_v1.csv'
$artifactDir = Join-Path $repoRoot 'simul_log_smoke\beta_zero_recovery_26092_26093'
$outputDir = Join-Path $repoRoot 'collected\ipmsm_v2_affinityfix_replay_v1'
$python = 'C:\Python314\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python interpreter is unavailable: $python"
}

$generatorArguments = @(
    '-u',
    (Join-Path $repoRoot 'generate_ipmsm_affinity_replay_pilot.py'),
    '--source-plan', $sourcePlan,
    '--output', $pilotPlan
)
if ($PreparePlan) {
    $generatorArguments += '--execute'
    & $python @generatorArguments
    exit $LASTEXITCODE
}
if (-not (Test-Path -LiteralPath $pilotPlan -PathType Leaf)) {
    & $python @generatorArguments
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
    if ($Submit) {
        throw 'Pilot plan is absent. Run the default dry-run, then -PreparePlan, then -Submit.'
    }
    Write-Host 'Source replay rows validated. Run -PreparePlan as a separate explicit step.'
    exit 0
}

$generatorArguments += '--verify-output'
& $python @generatorArguments
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$campaignArguments = @(
    '-u',
    (Join-Path $repoRoot 'run_ipmsm_profile_scoped_campaign.py'),
    '--expected-profile-count', 'time_138_p12_baseline=1',
    '--expected-profile-count', 'time_135_p12_iron525=1',
    '--cases', $pilotPlan,
    '--project', 'PYAEDT_MOTOR_IPMSM_V2',
    '--scheduler-url', 'http://127.0.0.1:8000',
    # The scheduler project itself is sealed at 100.  This pilot still runs at
    # most two cases because its verified plan contains exactly two rows.
    '--project-active-cap', '100',
    '--task-prefix', 'ipmsm-v2-affinityfix-replay-v1',
    '--remote-cases-dir', 'remote/ipmsm_v2_affinityfix_replay_v1',
    '--result-dir', 'simul_log/ipmsm_v2_affinityfix_replay_v1',
    '--simulation-dir', 'simulation/ipmsm_v2_affinityfix_replay_v1',
    '--log-dir', 'simul_log_scheduler/ipmsm_v2_affinityfix_replay_v1_logs',
    '--output-dir', $outputDir,
    '--merged-output', 'affinityfix_replay_v1_results.csv',
    '--beta-summary', (Join-Path $artifactDir 'beta_mtpa_summary.json'),
    '--beta-case-plan', (Join-Path $artifactDir 'beta_mtpa_cases.csv'),
    '--beta-results', (Join-Path $artifactDir 'beta_mtpa_collected_26094_26103\beta_mtpa_results.csv'),
    '--beta-calibration-manifest', (Join-Path $artifactDir 'beta_zero_manifest.json'),
    '--allowed-quality-profile', 'time_138_p12_baseline',
    '--allowed-quality-profile', 'time_135_p12_iron525',
    '--scheduling-profile', 'fea_bursty',
    '--required-capability', 'conda:pyaedt2026v1',
    '--env-profile', 'pyaedt2026v1',
    '--env-setup', 'module load ansys-electronics/v252',
    '--max-workers-per-node', '1',
    '--cpus', '4',
    '--cores-per-process', '4',
    '--completed-result-settle-seconds', '300',
    '--timeout', '30'
)
if ($Submit) {
    $campaignArguments += '--submit'
}

$logStem = if ($Submit) { 'affinityfix_replay_v1' } else { 'affinityfix_replay_v1.dryrun' }
$stdout = Join-Path $artifactDir "$logStem.stdout.log"
$stderr = Join-Path $artifactDir "$logStem.stderr.log"
& $python @campaignArguments 1>> $stdout 2>> $stderr
exit $LASTEXITCODE
