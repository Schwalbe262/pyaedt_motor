# IPMSM FEA와 최적설계 연구

8극·12슬롯 IPMSM의 형상과 운전조건을 바꾸어 Maxwell 2D 데이터를 만들고, 회귀모델과 FEA 재검증으로 설계를 비교합니다.

현재 개선 작업과 검증 결과는 [연구 점검 기록](docs/RESEARCH_AUDIT.md), 출력값의 뜻은 [파라미터 설명](docs/PARAMETERS.md), 새 실행을 재개할 때는 `HANDOFF_CURRENT.md`를 확인합니다.

참고자료는 [references](references/README.md), 이전 탐색 노트북은 [notebooks/legacy](notebooks/legacy/README.md), 정리한 실행본과 검증 백업의 위치는 [보관 대응표](docs/ARCHIVE_INDEX.md)에 있습니다. 노트북의 코드·설명은 보존했고 실행 출력은 D: 원본 백업에서 확인할 수 있습니다.

과거 최종설계의 분석·그림 도구는 [legacy/final_design_2026_07](legacy/final_design_2026_07/README.md)에 모았습니다. 원래 경로와 파일 해시, 복원 방법도 함께 보관합니다.

## 작업 위치

| 위치 | 용도 |
|---|---|
| `Y:\git\pyaedt_motor` | 코드, 설정, 테스트, 요약 보고서 |
| `C:\Users\peets\NEC\pyaedt_motor_runtime` | 실행별 고정 소스, 입력 계획, 로컬 해석 작업 |
| `D:\IPMSM_Archive` | 검증된 완료 결과, 이전 코드·수정본·실험 보관 |

`configs/ipmsm_7p5kw.json`은 현재 연구 사양입니다. 1200 rpm에서 59.683 Nm, 5000 rpm에서 7.5 kW를 요구하고 최대 직경은 300 mm입니다. 참고 PPT의 원래 수치는 별도로 보존합니다.

새 연구 사양은 공식 라이브러리의 `M19_29G`를 별도 기준 강판으로 사용합니다. 실습 PNF 강판과 다른 재료이며, `Y=2`는 명시한 기준 모델 가정입니다. 기존 PNF 해석 결과와 섞어 학습하지 않습니다. 재료 모델·권선 단면적·점적률 한도는 영점 보정부터 최종 FEA까지 전달하고, 실제 CAD 면적으로 해석 전에 점적률을 검사합니다.

## 실행

Python 3.11의 `pyaedt2026v1` 환경에서 다음을 실행합니다.

FEA 연동은 현재 검토·시험한 PyAEDT 0.22.0과 Ansys v252를 기준으로 고정합니다. 버전을 바꿀 때는 실제 모터 설정 시험을 다시 수행합니다.

```powershell
python -m pip install --no-deps --no-build-isolation -e .
python -m pyaedt_motor --help
python -m pyaedt_motor simulate --help
python -m pyaedt_motor plan --source <검증할결과.csv> --output <새실험계획.csv>
python -m pyaedt_motor doe --help
python -m pyaedt_motor quality --help
python -m pyaedt_motor.cluster --help
python -m pyaedt_motor audit --dataset <결과.csv> --output <점검요약.json>
python -m pyaedt_motor train --help
python -m pyaedt_motor optimize --help
python -m pyaedt_motor validate --help
python -m pyaedt_motor archive --source <완료폴더> --destination <D의새보관폴더>
```

해석은 C: 작업 영역 또는 클러스터에서 실행합니다. `PYAEDT_LIBRARY_ROOT`에는 해당 실행에서 사용할 `pyaedt_module`의 상위 폴더나 `pyaedt_library` 저장소를 지정할 수 있습니다. 기존 `run_ipmsm_batch.py`와 상위 실행 스크립트의 인자도 유지합니다.

Windows의 새 `simulate` 명령은 코드와 보조 라이브러리를 C:에 복사하고 파일별 해시를 검증한 뒤 그 실행본으로 해석합니다. 기본 프로젝트·결과 경로도 해당 C: 실행 폴더입니다. `train`과 `optimize`도 C:의 고정 소스를 새 Python 프로세스에서 사용하며 출력은 C: 경로로 지정합니다. `IPMSM_RUNTIME_ROOT`로 작업 상위 폴더를 지정할 수 있습니다.

새 실험계획과 모델·최적화 명령은 [데이터 생성](docs/DATASET_V3.md), [모델 학습](docs/PHYSICAL_MODEL.md), [설계 탐색](docs/PHYSICAL_OPTIMIZATION.md), [클러스터 실행](docs/CLUSTER_RUNS.md)을 참고합니다. 과거 `controller.py`와 v4 자동화는 이전 실행 재현용입니다. 현재 연구에는 새 `/api/tasks` 경로와 공유 시도 원장을 사용합니다.

계산 자원은 예약한 코어 수와 실제 solver 사용 기록을 구분해 확인합니다. 저장된 native `.profile`과 해당 실행의 ACF가 있으면 다음 명령으로 행렬 계산의 실제 코어 수·비선형 잔차를 점검합니다. 원본 증거와 해석 설정은 변경하지 않으며, 이 점검 결과만으로 정확도나 속도 개선을 승인하지 않습니다.

```powershell
python -m pyaedt_motor.solver_profile <실행.profile> --requested-cores 4 --acf <실행.acf> --output <C의점검결과.json>
```

## 품질과 실험 한도

- 신규 FEA는 실패·재시도를 포함해 총 3,000회 이내이며 진단 300회, 학습 데이터 2,400회, 최종 설계 검증 300회를 배정합니다.
- 클러스터의 프로젝트 전체 대기·실행 합계는 300 이하로 유지합니다. 먼저 설정 스모크와 소수 해석을 검증합니다.
- `screening`만 인덕턴스 계산을 생략할 수 있습니다. 정식 `training`과 `reference`는 Ld/Lq를 계산합니다.
- 설정 검증과 결과 품질 검증은 별개입니다. 정상상태·메시·시간 간격 수렴과 물리 정의를 확인한 데이터로 모델을 학습합니다.
- [공간 메시 제어](docs/MESH_CONTROLS.md): 기존 추가 요소 예산과 명시적 `length_only` 모드를 구분하고 실제 적용값을 검사합니다.
- 새 모델 승격에는 전압 span을 포함한 전체 10개 출력의 시험 R²≥0.95와 물리·출처 검사가 필요합니다. 현재 새 연구 모델과 최종 설계는 아직 승인하지 않았습니다.
- 보관 명령은 원본을 삭제하지 않습니다. 파일별 해시 검증과 사용 여부 확인 후 정리합니다.
