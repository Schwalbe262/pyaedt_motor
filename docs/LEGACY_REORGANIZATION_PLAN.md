# 과거 버전 정리의 의존성 점검

2026-10-30의 읽기 전용 점검입니다. 이 문서 작성 과정에서 브랜치·작업 트리·원격 배포·기존 소스는 변경하지 않았습니다. 현재 실행 중인 FEA는 고정 소스 `5914cb3`를 계속 사용해야 합니다.

## 먼저 분리할 작은 의존성

`calibrate_ipmsm_beta.py:141`의 `calibration_source_from_spec`가 형상 하나를 만들기 위해 `generate_ipmsm_v2_cases._valid_geometry_samples`를 가져옵니다. 해당 생성기는 27행에서 `continue_ipmsm_v2_stage2`를 가져오고, 이 파일의 동적 v4r9 분기와 고정 소스 목록을 통해 v4 자동화까지 연결됩니다. 새 DOE의 순수 `sample_feasible_geometries` 함수로 이 호출만 교체하면 보정 기능에서 과거 실행 체인을 분리할 수 있습니다. 기존 형상 샘플러의 제약은 양수 형상과 점적률 검사이며, 새 샘플러에서도 동일한 물리 제약을 보존해야 합니다.

`optimize_ipmsm_nsga2.py` 자체의 직접 의존성은 `atomic_publish`, `ipmsm_optimization`, `ipmsm_surrogate_bundle`입니다. 78~82행의 재개 지문도 이 세 최적화 소스만 대상으로 합니다. 628행의 동적 predictor는 사용자가 명시한 시험용 확장이고, 588행의 동적 의존성은 `pymoo`입니다. 이 실행기를 v4 의존성 때문에 일괄 이동할 필요는 없습니다.

다만 현재 최적화 실행기의 정식 모델 경로는 기존 `IPMSMV2SurrogateBundle`과 `reference_ultra`를 요구합니다(`optimize_ipmsm_nsga2.py:635`). 새 물리 모델 bundle과 새 수렴 프로파일을 사용할 때는 정식 adapter를 연결해야 합니다. `--predictor` 시험 경로로 우회해 검증된 설계라고 표시해서는 안 됩니다. `validate_ipmsm_pareto_fea.py`에도 같은 `reference_ultra` 계약이 있으므로 최종 설계 검증 경로를 함께 이관해야 합니다.

## 유지할 기능과 과거 도구

| 구분 | 유지·이관 대상 | 확인한 주의점 |
|---|---|---|
| 현재 FEA | `run_ipmsm_batch.py`, `subprocess_run.py`, `module/`, 재료 CSV | 실제 적용·출력 검증과 고정 실행본을 유지 |
| 학습·입력 처리 | `train_ipmsm_lightgbm.py`, `verify_regression_metrics.py`, 새 `src/pyaedt_motor/` 모델·DOE | 새 모델의 그룹 분리와 합격 검증을 완료한 뒤 CLI 연결 |
| 제어·최적화 수학 | `ipmsm_optimization.py`, `calibrate_ipmsm_beta.py`, 현행 optimizer·최종 FEA 검증기 | 보정용 순수 샘플러 분리, 새 bundle/profile adapter 필요 |
| 품질 비교 | `analyze_ipmsm_quality_results.py`, `rank_ipmsm_quality_profiles.py`, second/third-pass 계열 | 기본값을 새 실험에 그대로 쓰지 않음. 아래 설명 참조 |
| 수집·작업 제출 | `inspect_ipmsm_scheduler_job.py`, `fetch_ipmsm_aedt_projects.py`, `submit_ipmsm_scheduler_job.py`, `submit_ipmsm_scheduler_task.py` | 실제 사용 중인 기능은 `tools/` 또는 package로 이관 후 기존 경로 호환성 검사 |
| 과거 실행 orchestration | `continue_*_v4*`, `build_*_v4*`, `coordinate_*`, `supervise_*`, 과거 watcher·dashboard launcher | 현행 실행 호출이 없어짐을 확인한 뒤 과거 소스 tag와 검증된 실행 증거로 보관 |
| 프로젝트 운영 | `codex_ops.py`, 메모리 파일 | FEA import graph 밖에 있어도 프로젝트 정책이 참조하므로 보존 |

품질 분석기는 기본적으로 `output_*_all_*` 평균을 사용합니다(`analyze_ipmsm_quality_results.py:13`). 이는 초기 과도 구간까지 포함하므로 새 정상상태 비교에는 마지막 주기 지표를 명시해야 합니다. 분석기의 `--metrics`는 이를 지정할 수 있습니다. 순위 도구는 같은 전체 구간 지표와 토크 2%·효율 1.5%p 허용치를 하드코딩하고 있어 현행 토크 1%·효율 0.5%p 기준과 다릅니다(`rank_ipmsm_quality_profiles.py:15`). 또한 완료된 reference만 분모로 사용하므로 누락된 reference 그룹이 평가에서 사라질 수 있습니다(145~163행). 새 비교 도구는 case plan의 전체 기대 범위를 기준으로 누락·실패를 표시해야 합니다.

`controller.py`는 `simulation1.sh`를 통한 수동 호환 경로입니다. 기본 분석 반복은 예산 검사가 막지만, 명시적으로 허용해 실행하면 `scancel -u <사용자> --name=ANSYS`를 먼저 수행할 수 있습니다(60~64, 103~110, 178~190행). 현재 연구의 자동 실행은 프로젝트별 `/api/tasks` 경로로 유지합니다. 기존 파일을 보관할 때는 `goal.md`에 적힌 호환 진입점의 처리 방침도 함께 갱신해야 합니다.

`submit_ipmsm_profile_stage.py`의 제출 기본 경로는 이미 `/api/tasks`이며 `fea_bursty`, dedupe와 node-worker 인자를 전달합니다. 이 파일을 이름만 보고 오래된 `/tasks` 전용 도구로 분류해서는 안 됩니다.

## 브랜치와 남은 작업 트리

| 브랜치 | 점검 시 head | 현행 연구 브랜치 대비 |
|---|---|---|
| `main` | `2107a2a` | 조상. 검증 완료된 연구 변경으로 fast-forward 가능 |
| `chore/codex-context-budget` | `fdeabbe` | 조상 |
| `release/ipmsm-aedt-pool-v2-260715` | `deccea7` | 조상 |
| `release/ipmsm-keepalive-retry-260716` | `7a242c6` | 미병합 patch 0개, 같은 patch 1개 |
| `fix/ipmsm-report-delimiter-260716` | `7085184` | 미병합 patch 0개, 같은 patch 2개 |
| `fix/ipmsm-native-pipeline-barrier-260716` | `b624406` | 미병합 patch 1개: native pipeline barrier |
| `fix/ipmsm-mixed-canary-deploy-ready-260716` | `3f556a3` | 미병합 patch 7개, 같은 patch 2개. 과거 canary 승인·affinity·barrier 포함 |

`dc9e8ab` 전체 commit은 Git patch 비교에서 미병합으로 나타나지만, 메시 수정 두 파일은 이미 `319bd52`로 반영됐습니다. 나머지는 과거 canary runbook·authority·제출기 변경입니다. 이 commit 전체를 다시 적용하지 않습니다. `b624406`은 현행에 없는 별도 안전 기능이므로 정확한 소스와 테스트를 보관하고, pooled backend를 채택할 때 현행 scheduler와 다시 검증합니다. 현재 standalone FEA의 정상 실행 근거를 대신하지 않습니다.

남은 Y: 작업 트리 두 개와 C: native-barrier 작업 트리는 `git status --short`와 staged/unstaged diff stat에서 변경이 없었습니다. 이 사실은 **ignored 실행 결과의 백업 완료를 뜻하지 않습니다.** 각 트리의 ignored 증거·로그·설정까지 파일별 해시 archive로 확인한 뒤 정리합니다. D:로 옮긴 `retired/c18` 작업 트리는 이미 분리된 detached HEAD입니다.

원격 프로젝트 등록은 브랜치명이 아닌 SHA에 고정돼 있습니다. 기존 모터 프로젝트는 `deccea7ec676508e1aaef1eb098c99523901b1c9`, 새 소스 프로젝트는 `5914cb3b1ba2319ce99856d8b1aafd151da54291`이며 모두 `auto_pull=false`였습니다. 브랜치 이름 정리가 실행 중인 checkout을 바꾸지는 않지만, 이 두 SHA는 main 또는 보관 tag에서 계속 도달 가능해야 합니다.

## 보존 후 정리 순서

1. 현재 변경과 새 pipeline을 별도 checkpoint로 저장하고, 활성 CLI·import·실제 case dry-run 테스트를 통과시킵니다. 실행 중 원격 고정 소스는 갱신하지 않습니다.
2. 현재 ref/head를 기록한 manifest와 모든 ref bundle을 D:에 검증합니다. 남은 작업 트리의 ignored 파일과 동작 당시 설정은 bundle 외에 보관합니다.
3. 모든 과거 브랜치 head에 `archive/20261030/<기존 이름>` 형태의 annotated tag를 만들고, tag 원격 도달 가능성과 bundle 복원을 확인합니다. `main`의 기존 head도 기록합니다.
4. 원래부터 수정돼 있던 adaptive generator와 테스트를 별도로 보존합니다. 점검 당시 두 파일의 내용은 최초 `working_files/` 백업과 바이트 단위로 같았습니다. 이 변경은 실패 형상 복구 manifest의 원본 lineage와 effective plan을 구분하고 publisher를 재실행해 증거를 검증하는 기능입니다. 새 v3 DOE에 무조건 섞지 말고 복구 가능한 patch/source snapshot으로 유지합니다.
5. 필요한 작은 의존성 교체와 새 bundle/profile 연결을 완료한 뒤, 오래된 자동화의 참조가 현재 CLI·shell·테스트에 남지 않는지 검사합니다. `controller.py` 호환 경로를 폐기하거나 바꿀 경우 문서와 goal의 실행 경로도 갱신합니다.
6. 현행에서 사용하지 않는 과거 실행기와 해당 계약 테스트는 완전한 과거 소스 버전으로 보관합니다. 과거 테스트를 복원·재현하는 방법을 기록하고 고정 SHA·경로·승인 검사를 느슨하게 만들지 않습니다. 테스트 실패 여부만으로 보관 대상을 고르지 않습니다.
7. main에 검증된 현행을 반영하고, 과거 작업 트리를 detach/등록 정리한 뒤 중복 브랜치 ref를 정리합니다. 결과적으로 main과 현재 작업 브랜치를 남기고, 과거 버전은 tag+bundle+artifact index로 찾도록 합니다.

반복 삭제가 자동 검토에서 차단된 상황에서는 검증된 원본을 짧은 D: 보관 경로로 이동하는 가역적 방법을 유지합니다. Git 작업 트리의 드라이브 간 이동은 Git 자체 move가 실패할 수 있으므로 검증된 native move 뒤 owner 저장소의 `git worktree repair`가 필요합니다. 경로 길이와 RaiDrive UNC/Y: 별칭도 함께 확인합니다.

## 생성한 점검 자료

`C:\Users\peets\NEC\pyaedt_motor_runtime\rebuild_20261030`에 `legacy_dependency_inventory.json`, `branch_archive_inventory.json`, `branch_project_ref_dependencies.json`을 저장했습니다. 첫 파일은 import와 문자열 파일 참조를 포함한 보수적인 후보 목록이며, 실제 호출 문맥을 검토하기 전 자동 이동 목록으로 사용하면 안 됩니다.

## 새 마지막 주기 비교기의 검증 범위

- **기존 문제:** 전체 구간 평균과 완료 reference만을 기준으로 한 순위는 정상상태의 오차와 계획 누락을 구분하기 어렵습니다.
- **개선 아이디어:** case plan의 모든 기대 그룹을 유지하고, 마지막 주기의 9개 목표·동일 형상·전류·회전수·전기각 기준·재료·생성 코드를 확인합니다. 기준 해석과의 일치와, 독립적으로 세분화한 기준 해석까지의 일치를 나눕니다.
- **시도:** `src/pyaedt_motor/quality.py`와 `tests/test_ipmsm_v3_quality.py`를 추가했습니다. 합성 데이터에서는 누락·실패·중복·위조된 입력·에너지 항등식·선별용 해석·불충분한 세분화·출력 목표 생략이 합격으로 이어지지 않는지 확인했습니다. 현재 C:의 실제 수집 CSV에도 적용했습니다.
- **확인된 결과:** 비교기 테스트 12개와 기존 파형 검증을 합친 25개가 통과했습니다. 실제 부분 수집 39행에서는 기대 reference 6그룹의 누락을 모두 표시하고, 수정 설계 3건을 별도 미대응 사례로 남겼습니다. 이 데이터로 생산용 설정을 승인하지 않았습니다. 이는 도구의 동작 검증이며 메시 수렴이나 최적설계 완료의 증거가 아닙니다.
- **남은 검증:** 실제 reference 6건과 독립 세분화 결과가 모두 필요합니다. 새 재료 적용·새 형상 경계의 producer는 이전 source와 섞지 않고 비교합니다. 수정 설계의 정격·고속 운전 검증과 모델의 R² 합격은 별도 단계입니다.

비교기의 토크 평균·철손·고체손실·총손실·효율 기본 허용치는 각각 1%, 5%, 5%, 3%, 0.5%p입니다. 최대 토크·Ld·Lq·전압의 2% 기본값은 명시된 가정이며 조정할 수 있습니다. 손실과 토크의 상대 오차 분모에는 각각 1 W·1 Nm의 바닥값을 사용합니다. 실제 메시 요소 수와 시간 단계 수가 모두 증가한 독립 reference가 없으면 `candidate_relative_pass`만 평가하고 `production_profile_eligible`은 false로 유지합니다. 혼합 병렬 wave의 solve 시간 비율은 자원 경합의 영향을 받으므로 탐색용 지표입니다.

CLI 예시는 `python -m pyaedt_motor.quality --plan <cases.csv> --results <combined.csv> --reference-profile <name> --mesh-root <collected> --output <new-report.json>`입니다. 보고서는 분석한 입력 CSV와 mesh sidecar 바이트를 따로 보존합니다. 검증된 설정만 `--selected-profile`과 `--selected-output`으로 `ipmsm_v3_selected_profile_v1` manifest를 만들 수 있으며, 이 증거를 새 DOE에 연결합니다.
