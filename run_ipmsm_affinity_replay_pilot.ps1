[CmdletBinding()]
param(
    [switch]$PreparePlan,
    [switch]$Submit,
    [switch]$NoRedirect,
    [ValidateSet('paired', 'baseline', 'candidate')]
    [string]$Phase = 'baseline',
    [string]$NodeName = ''
)

$ErrorActionPreference = 'Stop'
if ($PreparePlan -and $Submit) {
    throw '-PreparePlan and -Submit must be separate operator steps.'
}
if ($NoRedirect -and -not $Submit) {
    throw '-NoRedirect is reserved for the submitted scheduled monitor.'
}
if ($Submit -and $Phase -eq 'paired') {
    throw 'Submitted affinity replays must run sequentially: use -Phase baseline or -Phase candidate.'
}
if ($Submit -and $Phase -eq 'candidate' -and [string]::IsNullOrWhiteSpace($NodeName)) {
    throw 'The submitted candidate phase requires -NodeName from the completed baseline task.'
}

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$sourcePlan = Join-Path $repoRoot 'simul_log_smoke\profile_thirdpass_speed_v2s1_paired24_cases_v1.csv'
$pilotPlan = Join-Path $repoRoot 'simul_log_smoke\profile_affinityfix_exclusive_seq_v2_cases.csv'
$artifactDir = Join-Path $repoRoot 'simul_log_smoke\beta_zero_recovery_26092_26093'
$python = 'C:\Python314\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python interpreter is unavailable: $python"
}

$generatorArguments = @(
    '-u',
    (Join-Path $repoRoot 'generate_ipmsm_affinity_replay_pilot.py'),
    '--source-plan', $sourcePlan,
    '--variant', 'exclusive-seq-v2',
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

$phaseConfig = switch ($Phase) {
    'baseline' {
        @{
            ExpectedProfiles = @('time_138_p12_baseline=1')
            Selection = @('--start', '1', '--limit', '1')
            OutputDir = Join-Path $repoRoot 'collected\ipmsm_v2_affinityfix_exclusive_seq_v2_baseline'
            MergedOutput = 'affinityfix_exclusive_seq_v2_baseline_results.csv'
        }
    }
    'candidate' {
        @{
            ExpectedProfiles = @('time_135_p12_iron525=1')
            Selection = @('--start', '2', '--limit', '1')
            OutputDir = Join-Path $repoRoot 'collected\ipmsm_v2_affinityfix_exclusive_seq_v2_candidate'
            MergedOutput = 'affinityfix_exclusive_seq_v2_candidate_results.csv'
        }
    }
    default {
        @{
            ExpectedProfiles = @(
                'time_138_p12_baseline=1',
                'time_135_p12_iron525=1'
            )
            Selection = @()
            OutputDir = Join-Path $repoRoot 'collected\ipmsm_v2_affinityfix_exclusive_seq_v2'
            MergedOutput = 'affinityfix_exclusive_seq_v2_results.csv'
        }
    }
}

$campaignArguments = @(
    '-u',
    (Join-Path $repoRoot 'run_ipmsm_profile_scoped_campaign.py')
)
foreach ($expectedProfile in $phaseConfig.ExpectedProfiles) {
    $campaignArguments += @('--expected-profile-count', $expectedProfile)
}
$campaignArguments += '--exclusive-node'
$campaignArguments += @(
    '--cases', $pilotPlan,
    '--project', 'PYAEDT_MOTOR_IPMSM_V2',
    '--scheduler-url', 'http://127.0.0.1:8000',
    # The scheduler project itself is sealed at 100. Phase selection is the
    # hard pilot-concurrency boundary; max_workers_per_node is only advisory.
    '--project-active-cap', '100',
    '--task-prefix', 'ipmsm-v2-affinityfix-exclusive-seq-v2',
    '--remote-cases-dir', 'remote/ipmsm_v2_affinityfix_exclusive_seq_v2',
    '--result-dir', 'simul_log/ipmsm_v2_affinityfix_exclusive_seq_v2',
    '--simulation-dir', 'simulation/ipmsm_v2_affinityfix_exclusive_seq_v2',
    '--log-dir', 'simul_log_scheduler/ipmsm_v2_affinityfix_exclusive_seq_v2_logs',
    '--output-dir', $phaseConfig.OutputDir,
    '--merged-output', $phaseConfig.MergedOutput,
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
$campaignArguments += $phaseConfig.Selection
if (-not [string]::IsNullOrWhiteSpace($NodeName)) {
    $campaignArguments += @('--node-name', $NodeName.Trim())
}
if ($Submit) {
    $campaignArguments += '--submit'
}

$logStem = if ($Submit) {
    "affinityfix_exclusive_seq_v2.$Phase"
} else {
    "affinityfix_exclusive_seq_v2.$Phase.dryrun"
}
$stdout = Join-Path $artifactDir "$logStem.stdout.log"
$stderr = Join-Path $artifactDir "$logStem.stderr.log"
if ($NoRedirect) {
    & $python @campaignArguments
} else {
    & $python @campaignArguments 1>> $stdout 2>> $stderr
}
exit $LASTEXITCODE
