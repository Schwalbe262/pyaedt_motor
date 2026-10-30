# 물리 기반 회귀모델 학습과 사용

`pyaedt_motor.train_physical`은 새 FEA 데이터로 최적설계용 모델 후보를 학습합니다. 기존 1,600행 진단 결과를 합격 모델로 승격하지 않습니다. 실제 새 데이터에 대한 학습·합격 판정은 별도 실행 결과를 확인해야 합니다.

학습 전에 명세의 고정 재료 모델·이름과 실제 FEA 행의 `input_core_material_model`, `input_core_material`을 대조합니다. M19 기준 모델은 실제 도체 단면적·점적률 한도도 명시해야 하며, 학습·보정·시험 행 전체에서 권선 명세와 일치해야 합니다. 계획 CSV의 값으로 누락된 실제 메타데이터를 대신하지 않습니다. 이 문맥 정보는 회귀모델의 입력 특징으로 추가하지 않습니다.

```powershell
& C:/Users/peets/anaconda3/envs/pyaedt2026v1/python.exe -m pyaedt_motor train --data C:\motor_run\accepted_results.csv --spec configs\ipmsm_7p5kw.json --output C:\motor_run\physical_model
```

On Windows, use the top-level `train` and `optimize` commands with the
`pyaedt2026v1` Python3.11 interpreter. They freeze root scripts and packaged
source onto C: before importing the production module in a fresh process.
Python3.14+ execution receives an explicit environment instruction. Runtime
roots and output paths must use native C: storage without reparse ancestors;
relative paths are normalized against the invoking directory before freezing.


입력은 평면 스키마 `ipmsm_v2`와 물리 지표 계약 `motor_metrics_v3`를 함께 사용합니다. 정상상태, 전력수지, 필수 출력, 단일 소스·재료·해석 설정·전기각 규약을 검사합니다. 실패 행·중복 행을 학습 명령 안에서 조용히 제거하지 않습니다. 비활성 변수 `stator_shoe_thick`는 1.5 mm로 고정합니다.

같은 실제 형상과 적층 길이를 다른 그룹 이름으로 나누는 것도 거부합니다. 그룹의 `train`·`calibration`·`test` 배정을 유지하고, 모델 종류는 학습 그룹 내부의 홀드아웃에서만 고릅니다. 선택 내용을 `selection.json`에 먼저 고정한 후 전체 시험 행을 한 번 평가합니다. 지원 범위를 벗어난 시험 행도 시험 지표에서 빼지 않습니다.

8개 기본 출력의 모델을 학습하고 총손실·효율은 물리 관계로 계산합니다. 평균·최대 토크, 철손·고체손실, Ld·Lq, 상전압 절대 피크·전압 span, 총손실, 효율의 10개 시험 R²가 모두 0.95 이상이고 예측값의 물리 검사와 불확실성 보정이 유효해야 `accepted`입니다. 미달 모델도 `rejected`로 보존하며 명령은 종료 코드 2를 반환합니다. 합격 모델도 최종 설계의 독립 FEA를 대신하지 않습니다.

현재 Ld·Lq는 Maxwell의 겉보기 인덕턴스 행렬을 보정된 dq 좌표계로 변환한 대각 성분입니다. 이 행렬은 해당 운전점의 절대 투자율을 고정하고 영구자석 자체의 자속을 제외해 계산합니다. 제어기의 작은 전류 변화에 대한 응답 모델에는 증분 인덕턴스와 dq 교차 성분도 별도로 확인해야 합니다. 원시 3×3 행렬은 함께 보존합니다. 정의와 계산 조건은 [Ansys v252 인덕턴스 설정](https://ansyshelp.ansys.com/public/Views/Secured/Electronics/v252/en/Subsystems/Maxwell/Content/Maxwell/MatrixComputationSettingsforTransientSolutions.htm)을 참고합니다.

인덕턴스의 기본 주기 통계는 경계 시각을 보간하고 시간 적분해 구합니다. 마지막 주기가 불완전하거나 비유한 값을 포함하면 필수 Ld·Lq는 NaN으로 남겨 후속 품질 검사가 거부하도록 합니다. `summarize_inductance_matrix(..., integrate_cycles=False)`는 단일 시점 등 샘플 진단에 명시적으로 사용하는 옵션입니다. 기존 고정 실행본과 원시 결과는 보존하며 새 통계 코드의 지문을 구분합니다.

`output_voltage_span_last_peak_v`는 각 시각의 세 상전압 최댓값과 최솟값 차이를 구한 뒤 마지막 주기에서 취한 피크입니다. 양수·유한값과 `span <= 2 × 상전압 절대 피크`를 검사합니다. min-max PWM의 인버터 전압 판정에는 이 span의 불확실성 상한과 `utilization × Vdc`를 비교합니다. 예를 들어 200 V·0.95이면 한계는 190 V입니다. 상전압 절대 피크도 별도 출력으로 유지합니다. 기존의 7개 기본 출력만 가진 아티팩트는 새 계약에서 사용할 수 없습니다. 생산 출력 계약은 `model_bundle.PRODUCTION_TARGETS`와 `ALL_TARGETS`에 정의되며 아티팩트 스키마는 `ipmsm_physical_surrogate_v2`입니다.

불확실성은 학습 데이터만으로 정한 출력 변환·표준화에 적용합니다. 보정 형상마다 모든 운전점·8개 출력의 최대 오차를 하나의 점수로 만들고, 형상 수를 기준으로 95% 유한표본 순서통계량을 선택합니다. 총손실과 효율 구간은 기본 출력 구간으로부터 보수적으로 전파합니다. 일반 split-conformal의 순서통계량과 교환가능성 조건은 [Angelopoulos·Bates의 설명](https://arxiv.org/html/2107.07511v6)을 참고했습니다. 형상 전체를 하나의 점수 단위로 묶는 것은 본 구현의 설계입니다. 이 값은 형상 그룹의 교환가능성을 전제로 한 명목상 주변 보장이고, 최적화가 선택한 입력이나 분포가 달라진 입력에 대한 정확도 보장은 아닙니다.

모델은 관측한 학습 입력의 좌표별 범위를 벗어나는 외삽을 거부합니다. 범위 안에서도 모든 변수 조합을 촘촘하게 검증했다는 뜻은 아닙니다. 선언된 형상 범위, 8극·12슬롯, 학습한 회전수, 파생 치수, 점적률, 권선과 적층 길이로 재계산한 100°C 상저항도 확인합니다. 입력은 저장된 `domain.input_columns` 순서의 FEA 입력 열을 포함하는 DataFrame을 전달합니다.

```python
from pyaedt_motor.model_bundle import load_bundle

model = load_bundle("C:/motor_run/physical_model/artifact",
                    expected_fingerprints=expected_fea_fingerprints)
point, lower, upper = model.predict(query_inputs)
```

최적설계용 로드는 예상한 소스·재료·프레임 등의 지문을 명시해야 합니다. 거부된 모델을 진단에만 열려면 `require_approved=False`, 예측 시에도 `allow_rejected=True`를 각각 명시합니다. 최적설계의 기본 경로는 합격하지 않은 모델을 사용하지 않습니다.

`artifact`에는 모델, 메타데이터, 원문 소스 사본과 파일별 해시가 있습니다. `COMPLETE`를 마지막에 써서 발행하며 기존 출력 폴더는 덮어쓰지 않습니다. 로드는 검증한 동일 바이트를 역직렬화하고, 입력 열·코드·설치 패키지 버전 변경을 거부합니다. 실행 소스 지문은 Python이 동등하게 취급하는 UTF-8 BOM과 줄바꿈만 정규화합니다. 원문 사본의 파일 해시는 바이트 그대로 검증합니다. 기존 고정 해시 계약은 변경하지 않습니다.

상위 실행 폴더에는 고정한 선택, 시험 예측·지표, 사용 사양과 학습 코드 사본이 남습니다. 명령행 실행은 읽은 데이터 바이트도 고정하고 SHA-256과 `source_dataset.csv`를 보존합니다.

실제 FEA의 상 직렬 턴수와 코일 측면 턴수도 숫자 특징과 별도로 저장하고 선언한 전체 권선 사양과 대조합니다. 다른 권선의 행을 혼합하거나 예측 입력에 다른 턴수를 지정하면 거부합니다. 현재 FEA 구조는 병렬 분기 1개와 상별 코일 4개를 지원합니다.

학습 시작 시 예측 코드·전처리 코드·패키지 버전을 고정하고, 발행 직전까지 변경되지 않았는지 확인합니다. 실행 중 파일이 바뀌면 새 코드의 지문으로 이전 계산 결과를 발행하지 않습니다. 오래 실행한 Python 프로세스의 로드된 예측 코드와 디스크의 코드가 달라진 경우에도 로드를 거부합니다.

합성 모터 관계를 사용한 테스트는 데이터 분리, 원래 입력 보존, 불확실성 전파와 저장·로드 절차를 검증합니다. 이 테스트의 통과는 실제 FEA 데이터에 대한 회귀 성능 개선이나 연구 모델의 합격을 뜻하지 않습니다.
