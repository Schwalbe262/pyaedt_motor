# IPMSM FEA와 최적설계 연구

8극·12슬롯 IPMSM의 형상과 운전조건을 바꾸어 Maxwell 2D 데이터를 만들고, 회귀모델과 FEA 재검증으로 설계를 비교합니다.

현재 개선 작업과 검증 결과는 [연구 점검 기록](docs/RESEARCH_AUDIT.md), 출력값의 뜻은 [파라미터 설명](docs/PARAMETERS.md), 새 실행을 재개할 때는 `HANDOFF_CURRENT.md`를 확인합니다.

## 작업 위치

| 위치 | 용도 |
|---|---|
| `Y:\git\pyaedt_motor` | 코드, 설정, 테스트, 요약 보고서 |
| `C:\Users\peets\NEC\pyaedt_motor_runtime` | 실행별 고정 소스, 입력 계획, 로컬 해석 작업 |
| `D:\IPMSM_Archive` | 검증된 완료 결과, 이전 코드·수정본·실험 보관 |

`configs/ipmsm_7p5kw.json`은 현재 연구 사양입니다. 1200 rpm에서 59.683 Nm, 5000 rpm에서 7.5 kW를 요구하고 최대 직경은 300 mm입니다. 참고 PPT의 원래 수치는 별도로 보존합니다.

## 실행

Python 3.11의 `pyaedt2026v1` 환경에서 다음을 실행합니다.

```powershell
python -m pip install --no-deps --no-build-isolation -e .
python -m pyaedt_motor --help
python -m pyaedt_motor simulate --help
python -m pyaedt_motor plan --source <검증할결과.csv> --output <새실험계획.csv>
python -m pyaedt_motor audit --dataset <결과.csv> --output <점검요약.json>
python -m pyaedt_motor train --help
python -m pyaedt_motor optimize --help
python -m pyaedt_motor validate --help
python -m pyaedt_motor archive --source <완료폴더> --destination <D의새보관폴더>
```

해석은 C: 작업 영역 또는 클러스터에서 실행합니다. `PYAEDT_LIBRARY_ROOT`에는 해당 실행에서 사용할 `pyaedt_module`의 상위 폴더나 `pyaedt_library` 저장소를 지정할 수 있습니다. 기존 `run_ipmsm_batch.py`와 상위 실행 스크립트의 인자도 유지합니다.

Windows의 새 `simulate` 명령은 코드와 보조 라이브러리를 C:에 복사하고 파일별 해시를 검증한 뒤 그 실행본으로 해석합니다. 기본 프로젝트·결과 경로도 해당 C: 실행 폴더입니다. `IPMSM_RUNTIME_ROOT`로 작업 상위 폴더를 지정할 수 있습니다.

## 품질과 실험 한도

- 신규 FEA는 실패·재시도를 포함해 총 3,000회 이내이며 진단 300회, 학습 데이터 2,400회, 최종 설계 검증 300회를 배정합니다.
- 클러스터의 프로젝트 전체 대기·실행 합계는 300 이하로 유지합니다. 먼저 설정 스모크와 소수 해석을 검증합니다.
- `screening`만 인덕턴스 계산을 생략할 수 있습니다. 정식 `training`과 `reference`는 Ld/Lq를 계산합니다.
- 설정 검증과 결과 품질 검증은 별개입니다. 정상상태·메시·시간 간격 수렴과 물리 정의를 확인한 데이터로 모델을 학습합니다.
- 모델 승격에는 주요 8개 출력과 전압의 시험 R²≥0.95가 필요합니다. 미달 모델의 최적화 결과는 검증 완료 설계로 표시하지 않습니다.
- 보관 명령은 원본을 삭제하지 않습니다. 파일별 해시 검증과 사용 여부 확인 후 정리합니다.
