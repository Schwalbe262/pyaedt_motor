# 이전 재해석 분석 도구

과거 replay 결과의 실패 패턴·출력 이상치·변화량을 분석하는 도구 3개와 품질 작업 명령을 만드는 도구 1개를 모았습니다. 새 M19 모델의 수렴이나 학습 성능을 승인하는 도구는 아닙니다. 현재 연구에는 `python -m pyaedt_motor quality`, `doe`, `train`을 사용합니다.

도구와 일반 테스트 각 4개를 원래 바이트 그대로 옮겼습니다. CRLF도 보존하며, 원래 경로·SHA256·Git blob·독립 백업 위치는 [source_map.csv](source_map.csv)에 있습니다. 이동 전 백업 `D:/IPMSM_Archive/ra0` 및 이동 후 파일을 모두 SHA로 대조했습니다.

프로젝트 루트에서 Python 3.11 `pyaedt2026v1` 환경으로 실행합니다. 남겨 둔 루트 보조 모듈과 명령을 사용하므로 작업 폴더와 입력 경로를 명시해야 합니다.

```powershell
$env:PYTHONPATH = ".;src"
python legacy/replay_analysis/analyze_ipmsm_failure_patterns.py --help
python legacy/replay_analysis/analyze_ipmsm_output_outliers.py --help
python legacy/replay_analysis/analyze_ipmsm_replay_drift.py --help
python legacy/replay_analysis/plan_ipmsm_quality_workflow.py --help
python -m pytest legacy/replay_analysis/test_analyze_ipmsm_failure_patterns.py legacy/replay_analysis/test_analyze_ipmsm_output_outliers.py legacy/replay_analysis/test_analyze_ipmsm_replay_drift.py legacy/replay_analysis/test_plan_ipmsm_quality_workflow.py --import-mode=prepend -q
```

이 테스트 4개 파일은 기본 `tests/` 경로에서 옮겼으며, 위 명령으로 별도 검증합니다. 이동 전·후 C 복사본에서 같은 19개 테스트를 통과했습니다. 수치 기준과 assertion은 수정하지 않았습니다. `plan_*`은 과거 루트 보조 명령의 실행 계획을 만들므로 현재 v3 FEA 계획으로 사용하지 않습니다.

원래 배치가 필요하면 별도 복원 작업공간에 해당 Git 버전을 준비하고 `source_map.csv`의 원래 경로로 파일을 복원한 뒤 SHA를 확인합니다. 현재 루트의 다른 보호 대상 코드나 사용자가 수정 중인 adaptive 소스·테스트는 옮기지 않았습니다.
