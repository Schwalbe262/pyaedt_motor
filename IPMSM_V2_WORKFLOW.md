# IPMSM v2 실행 워크플로

이 경로는 기존 legacy CSV와 분리한다. `full_360`, `symmetry_factor=1`,
`dq_current_advance_v2`, 동일 setup/material/AEDT fingerprint를 만족한 새 결과만
v2 surrogate 학습에 사용한다.

## 1. 모터 사양 준비

`ipmsm_motor_spec.example.json`을 복사하고 실제 정격점, 권선, 인버터, 전류밀도와
설계범위를 입력한다. 권선은 아래 항등식을 만족해야 한다.

```text
series_turns_per_phase * parallel_branches
  = turns_per_coil_side * coils_per_phase
```

초기 사양에는 `beta_calibration`이 없어도 된다.

## 2. 물리적 dq zero 보정

ElectricalZero는 부하 토크 최대점으로 정하지 않는다. 그렇게 하면 IPMSM의 MTPA
advance가 zero에 섞인다. 먼저 무부하 signed back-EMF 위상으로 d/q 기준을 고정한다.

```powershell
python calibrate_ipmsm_beta.py zero-generate `
  --spec ipmsm_motor_spec.json `
  --geometry-seed 42 `
  --rpm-values 600,1200 `
  --output beta_zero_cases.csv

python run_ipmsm_batch.py `
  --cases beta_zero_cases.csv `
  --result-csv beta_zero_results.csv `
  --simulation-dir simulation_beta_zero `
  --workers 1 --cores 4 --max-cases 200 --analyze

python calibrate_ipmsm_beta.py zero-analyze `
  --results beta_zero_results.csv `
  --manifest beta_zero_manifest.json

python calibrate_ipmsm_beta.py apply-manifest `
  --spec ipmsm_motor_spec.json `
  --calibration-manifest beta_zero_manifest.json `
  --output ipmsm_optimization_spec.json
```

여러 속도에서 추정한 zero가 기본 3도 이내로 일치하지 않으면 calibration은 실패한다.

부하 beta/MTPA는 zero를 바꾸지 않는 별도 검증이다.

```powershell
python calibrate_ipmsm_beta.py beta-generate `
  --spec ipmsm_motor_spec.json --geometry-seed 42 `
  --calibration-manifest beta_zero_manifest.json `
  --rpm 1200 --i-peak-a 100 `
  --beta-values=-10,0,10,20,30,40,50,60,70,80 `
  --output beta_mtpa_cases.csv

python calibrate_ipmsm_beta.py beta-analyze `
  --results beta_mtpa_results.csv `
  --calibration-manifest beta_zero_manifest.json `
  --summary beta_mtpa_summary.json
```

## 3. v2 foundation DOE와 FEA

기본 계획은 160 geometry group, 운전점별 3개 control sample, 40 repeat다.
현재 2개 운전점 사양에서는 geometry마다 no-load 1개와 부하 6개가 생성되므로
총 1,160 case다. geometry group 단위의 train/calibration/test split과 각 split의
current/beta boundary anchor가 CSV에 미리 기록된다.

```powershell
python generate_ipmsm_v2_cases.py `
  --spec ipmsm_optimization_spec.json `
  --output ipmsm_v2_foundation_cases.csv `
  --geometry-count 160 `
  --samples-per-operating-point 3 `
  --repeat-count 40 `
  --quality-profile reference_ultra `
  --max-cases 1200
```

FEA는 scheduler project `PYAEDT_MOTOR_IPMSM_V2`에서 한 번에 최대 100개
queued/attaching/running case만 유지한다. `/api/tasks`,
`scheduling_profile=fea_bursty`, `required_capability=conda:pyaedt2026v1`,
`env_profile=pyaedt2026v1`, `module load ansys-electronics/v252`를 사용한다.
각 case는 독립 task, remote case CSV, result path와 deterministic dedupe key를
사용한다. 동일 명령을 다시 실행하면 active/completed exact dedupe는 건너뛰고
failed/cancelled case만 재시도한다. 먼저 `--submit` 없이 manifest를 확인한다.

```powershell
python submit_ipmsm_v2_campaign.py `
  --cases ipmsm_v2_foundation_cases.csv `
  --project PYAEDT_MOTOR_IPMSM_V2 `
  --project-active-cap 100 `
  --start 1 --limit 196 `
  --task-prefix ipmsm-v2-foundation-w1 `
  --remote-cases-dir remote/ipmsm_v2_foundation_w1 `
  --result-dir simul_log/ipmsm_v2_foundation_w1 `
  --simulation-dir simulation/ipmsm_v2_foundation_w1 `
  --timeout-seconds 43200 `
  --write-manifest simul_log/ipmsm_v2_foundation_w1_manifest.json
```

검토 후 같은 명령 끝에 `--submit`을 붙인다. 활성 task가 끝날 때마다 같은 명령을
재실행하면 남은 case만 최대 100개까지 채운다. `reference_ultra`의 기존 평균
runtime이 6시간을 넘으므로 6시간 timeout을 재사용하지 않는다.

선택한 window의 모든 task가 성공 종료된 뒤 동일 identity 인자로 결과를 회수한다.
collector는 scheduler history coverage, task exit code, case ID, design hash, control
입력, setup/material/AEDT fingerprint를 먼저 검증하고 전부 유효할 때만 로컬
directory와 plan-order merged CSV를 만든다.

```powershell
python collect_ipmsm_v2_campaign.py `
  --cases ipmsm_v2_foundation_cases.csv `
  --project PYAEDT_MOTOR_IPMSM_V2 `
  --start 1 --limit 196 `
  --task-prefix ipmsm-v2-foundation-w1 `
  --remote-cases-dir remote/ipmsm_v2_foundation_w1 `
  --result-dir simul_log/ipmsm_v2_foundation_w1 `
  --simulation-dir simulation/ipmsm_v2_foundation_w1 `
  --output-dir collected/ipmsm_v2_foundation_w1 `
  --merged-output merged_results.csv
```

## 4. 데이터 gate와 surrogate 학습

모든 batch를 case ID 중복 없이 하나의 성공 결과 CSV로 합친 뒤 검증한다. merge는
case plan의 전 행이 정확히 한 번 `ok`로 존재하지 않으면 실패한다.

```powershell
python merge_ipmsm_v2_results.py `
  --case-plan ipmsm_v2_foundation_cases.csv `
  --input result_batch_001.csv result_batch_002.csv result_batch_003.csv `
          result_batch_004.csv result_batch_005.csv `
  --output ipmsm_v2_training_results.csv

python validate_ipmsm_v2_dataset.py `
  --data ipmsm_v2_training_results.csv `
  --summary ipmsm_v2_validation.csv

python train_ipmsm_lightgbm.py --v2 `
  --data ipmsm_v2_training_results.csv `
  --model-dir ipmsm_v2_models `
  --verification-output ipmsm_v2_r2_gate.csv `
  --r2-threshold 0.95 --fail-on-threshold `
  --ensemble-size 5 `
  --conformal-coverage 0.95 `
  --max-invalid-training-rows 0 `
  --max-removed-output-outlier-rows 0
```

검증기는 IQR로 물리적 극단을 삭제하지 않으며, fingerprint, split/repeat 무결성,
실제 3상 전류/전압, Id/Iq, 반복행 drift, 손실·효율 항등식을 검사한다.

학습은 preassigned geometry split을 그대로 사용하고, outer calibration은 tuning과
early stopping에서 격리한다. 다중 seed ensemble의 평균으로 6개 primitive와 2개
derived test R2를 계산하며, 8개 모두 0.95 이상인
bundle만 optimizer가 읽을 수 있다. 전압은 별도 auxiliary model이며 conformal UCB를
전압 제약에 사용한다.

R2 gate가 실패하면 임계값을 낮춰 optimizer로 넘어가지 않는다. seed와 prefix를 바꾸고
이전 plan의 design hash를 제외한 coverage batch를 추가한 뒤 전체 데이터를 다시 학습한다.

```powershell
python generate_ipmsm_v2_cases.py `
  --spec ipmsm_optimization_spec.json `
  --output ipmsm_v2_foundation_batch2.csv `
  --case-prefix v2b2 --seed 43 `
  --exclude-case-plan ipmsm_v2_foundation_cases.csv `
  --geometry-count 160 --samples-per-operating-point 3 `
  --repeat-count 40 --quality-profile reference_ultra
```

## 5. NSGA-II와 FEA 재검증

```powershell
python -m pip install -r requirements-optimization.txt

python optimize_ipmsm_nsga2.py `
  --spec ipmsm_optimization_spec.json `
  --model-dir ipmsm_v2_models `
  --dry-run

python optimize_ipmsm_nsga2.py `
  --spec ipmsm_optimization_spec.json `
  --model-dir ipmsm_v2_models `
  --output-dir ipmsm_optimization_output
```

목적함수는 active cylindrical volume과 `1 - duty-weighted cycle efficiency`다.
각 운전점에서 current/beta inner search를 수행하고 torque/power LCB, loss/voltage UCB,
전류, 전류밀도, slot fill, train-domain 제약을 적용한다. 생성된
`fea_validation_cases.csv`는 `reference_ultra`로 다시 풀어 Pareto 예측을 검증한다.

## 6. mesh/time 단축 판단

새 profile은 동일 geometry/control의 `reference_ultra`와 고정 비교한다. 현재 gate는
torque 2%, core/solid loss 5%, total loss 3%, Ld/Lq 3%, torque ripple 5 percentage
points, efficiency 1.5 points의 p90 오차와 runtime ratio 1.2 이하, complete group rate
0.95 이상이다. `rank_ipmsm_quality_profiles.py --fail-if-no-production-candidate`가
통과하기 전에는 v2 ground truth profile을 더 빠른 profile로 바꾸지 않는다.
