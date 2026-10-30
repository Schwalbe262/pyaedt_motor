# 새 학습 데이터의 실험계획

`src/pyaedt_motor/doe.py`는 해석 입력 CSV와 해시 manifest를 생성합니다. 클러스터 작업을 제출하거나 수렴·모델 합격을 판정하지 않습니다. CLI 연결은 상위 `pyaedt_motor` 명령에서 관리합니다.

기본 계획은 **720개 형상 × 3개 운전점 = 2,160건**입니다. 학습 단계의 총 2,400회 시도 중 240회는 실패·재시도용으로 남깁니다. 실행기는 전체 모터 작업의 대기·실행 합계를 300 이하로 유지하며 나누어 제출해야 합니다. 생성 manifest의 분할 크기는 300건씩 7회와 마지막 60건입니다.

각 형상은 1200 rpm과 5000 rpm을 모두 포함하고 세 번째 운전점의 속도를 교대로 배정합니다. 전체 속도별 행 수는 같거나 한 행 차이입니다. 전류는 기본적으로 유효 최대 피크 전류의 25~100%, β는 사양의 범위 안에서 표본화합니다. 학습 형상 안에 두 속도의 전류·β 경계 조건도 배치합니다. 0 A 무부하 해석은 전기각 영점 보정용이며 이 부하 모델의 학습 계획에 섞지 않습니다.

형상별 분할은 기본적으로 학습 432개, 보정 144개, 시험 144개입니다. 같은 형상의 세 운전점을 서로 다른 분할에 넣지 않습니다. 작은 시험 계획에서는 정수 개수로 반올림한 비율을 사용합니다. 시험·보정 출력은 표본 선택이나 모델 선택에 사용하지 않습니다.

현재 CSV 배치는 기존 `dataset_schema_version=ipmsm_v2`를 유지합니다. v3는 새 파형·물리량 계약인 `motor_metrics_v3`를 가리킵니다. 출력 행의 실제 생산자·메시·재료 지문은 FEA 실행기가 계산합니다. 계획에 예전 출력 지문이나 계산된 시간 간격을 복사하지 않습니다.

## 입력과 근거

다음 입력을 명시해야 합니다.

- `configs/ipmsm_7p5kw.json`과 같은 모터 사양. 생산 계획은 8극·12슬롯, 직경 300 mm 이하, 적층 40~70 mm, Vdc 200 V, 피크 전류 137.8 A 이하, 전류밀도 6.36 A/mm² 이하, 점적률 0.45 이하를 요구합니다.
- 내용 해시가 유효한 전기각 영점 manifest. `metric_contract=motor_metrics_v3`, `back_emf_source=periodic_flux_derivative`가 필요합니다. 과거 후진 차분 전압으로 만든 보정 결과는 받아들이지 않습니다.
- 선택한 프로파일 manifest. 아래 필드 형식을 사용하며 설정값과 근거 파일은 실제 수렴 비교 결과에서 가져옵니다.
- 이전 실험 CSV 목록. `--exclude`를 여러 번 사용하며, 기존 그룹 이름 대신 실제 형상 입력으로 중복을 검사합니다. 형상 입력이 누락된 이전 파일은 중복 여부를 증명할 수 없으므로 거부합니다.

프로파일 manifest의 `schema_version`은 `ipmsm_v3_selected_profile_v1`이고 다음 필드를 포함합니다.

| 필드 | 값 |
|---|---|
| `metric_contract_version` | `motor_metrics_v3` |
| `producer_fingerprint` | 선택 결과에 대응하는 실제 `producer_v3:sha256:...` 지문 |
| `settings.quality_profile` | 명시적인 프로파일 이름 |
| `settings.transient_periods`, `settings.steps_per_period` | 검증 후 선택한 정수 값 |
| `settings.mesh_elements` | `magnet`, `rotor`, `stator`, `winding`, `band`의 다섯 정수 값 |
| `settings.mesh_maximum_lengths_mm` | 선택 사항. 그룹별 양의 최대 요소 길이 |
| `settings.band_circle_segments` | 선택 사항. 실행기가 지원하고 근거에서 선택한 값 |
| `evidence` | 하나 이상의 `{path, sha256}` 기록. 상대 경로는 manifest 폴더 기준 |

학습 설정은 실제 모터 원호 `circle_segments=0`, `loss_policy=laminated_core`, `strict_setup=true`, `require_steady_state=true`, `compute_inductance=true`, `analysis_purpose=training`으로 고정합니다. 이 항목을 다르게 지정하면 거부합니다. 선택한 설정이 실제 실행기의 `build_spec`에서 적용되는지도 확인하므로 무시되는 옵션을 기록만 하고 넘어가지 않습니다.

근거 파일의 바이트 해시는 확인하지만, 생성기가 그 파일의 수치 수렴을 인증하지는 않습니다. 결과 manifest에도 `profile_convergence_certified_by_generator=false`를 기록합니다. 프로파일 선택과 새 코드 생산자의 적합성은 실제 해석 비교로 별도 검증해야 합니다.

## 형상과 재현성

후보는 고정된 크기의 Latin hypercube 묶음으로 만듭니다. 기존 `geometry_metrics`의 양의 형상 치수와 명시된 점적률 제한, 이미 계획된 형상만 제외합니다. 제약 조건을 적용한 최종 표본이 완전한 Latin hypercube라고 주장하지 않습니다.

비활성 `stator_shoe_thick`는 1.5로 고정하고 물리 형상 해시에서 제외합니다. 활성 14개 형상 변수와 적층 길이가 형상 그룹을 결정합니다. 활성 입력은 소수 12자리, 적층 길이는 현재 FEA 설정의 유효숫자 6자리로 정규화합니다. 전류·β·속도·파형 결과·이전 그룹명은 형상 식별에 사용하지 않습니다. 저항은 형상·적층 길이·권선 사양에서 100°C 기준으로 다시 계산합니다.

`sample_feasible_geometries(spec, count, seed, excluded_hashes=...)`는 `(design, stack_length_mm, physical_sha256)` 목록을 반환합니다. 이 순수 표본화 함수에는 영점·프로파일이나 과거 캠페인 모듈이 필요하지 않습니다. 영점 보정 전에 기준 형상을 만들 때도 사용할 수 있으며, 생산 계획의 지름 제한은 상위 생성 단계에서 적용합니다.

계획 CSV와 `.manifest.json`은 기존 파일을 덮어쓰지 않습니다. `verify_training_plan(path)`는 CSV·manifest 해시, 시도 예산, 형상 그룹 정체성과 분할 누출을 검사합니다. 두 파일에 걸친 게시가 원자적인 것은 아니므로 중단된 생성은 부분 파일을 남길 수 있습니다. 실행기는 둘을 검증한 뒤에만 사용하고, 중단된 파일을 자동 덮어쓰지 않아야 합니다.

모든 입력 해시, 라이브러리 버전, 생성기 소스 해시와 분할이 manifest에 남습니다. 입력 형상 제약을 통과하는 것은 메시 성공·정상상태·인버터 전압 충족을 뜻하지 않습니다. 실제 결과 품질 검사와 재시도 예산 관리는 실행 단계의 책임입니다.
