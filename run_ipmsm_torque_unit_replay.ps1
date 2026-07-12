[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$stateDir = Join-Path $repoRoot 'simul_log_smoke\v4r4'
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python environment is unavailable: $python"
}

New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
$stdout = Join-Path $stateDir 'torque_unit_replay_supervisor_v2.stdout.log'
$stderr = Join-Path $stateDir 'torque_unit_replay_supervisor_v2.stderr.log'
$pidFile = Join-Path $stateDir 'torque_unit_replay_supervisor.pid'
$arguments = @(
    'run_ipmsm_v2_campaign.py',
    '--cases', 'simul_log_smoke/v4r4/torque_unit_replay_plan_sealed.csv',
    '--project', 'PYAEDT_MOTOR_IPMSM_V2',
    '--project-active-cap', '50',
    '--max-plan-cases', '4',
    '--history-limit', '10000',
    '--task-prefix', 'ipmsm-v2-torqueunit-replay-v1',
    '--remote-cases-dir', 'remote/ipmsm_v2_torqueunit_replay_v1_cases',
    '--result-dir', 'simul_log_scheduler/ipmsm_v2_torqueunit_replay_v1_results',
    '--simulation-dir', 'simulation/ipmsm_v2_torqueunit_replay_v1',
    '--log-dir', 'simul_log_scheduler/ipmsm_v2_torqueunit_replay_v1_logs',
    '--env-setup', 'module load ansys-electronics/v252',
    '--required-capability', 'conda:pyaedt2026v1',
    '--env-profile', 'pyaedt2026v1',
    '--scheduling-profile', 'fea_bursty',
    '--max-workers-per-node', '1',
    '--keep-projects',
    '--beta-summary', 'simul_log_smoke/beta_zero_recovery_26092_26093/beta_mtpa_summary.json',
    '--beta-case-plan', 'simul_log_smoke/beta_zero_recovery_26092_26093/beta_mtpa_cases.csv',
    '--beta-results', 'simul_log_smoke/beta_zero_recovery_26092_26093/beta_mtpa_collected_26094_26103/beta_mtpa_results.csv',
    '--beta-calibration-manifest', 'simul_log_smoke/beta_zero_recovery_26092_26093/beta_zero_manifest.json',
    '--poll-interval-seconds', '30',
    '--overall-timeout-seconds', '604800',
    '--terminal-retry-limit', '1',
    '--completed-result-settle-seconds', '300',
    '--output-dir', 'simul_log_smoke/v4r4/torque_unit_replay_collected',
    '--submit'
)

try {
    [IO.File]::WriteAllText($pidFile, "$PID`n", [Text.UTF8Encoding]::new($false))
    Push-Location -LiteralPath $repoRoot
    # Direct invocation preserves the spaced module command as one argv value.
    # Windows PowerShell promotes native stderr to a terminating error while
    # the script-wide preference is Stop, but runner status is written there.
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & $python @arguments 1>> $stdout 2>> $stderr
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousErrorActionPreference
}
finally {
    Pop-Location
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}
exit $exitCode
