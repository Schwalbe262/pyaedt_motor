# 2026년 7월 최종설계 분석 도구 보관

이 폴더는 과거 p174/legacy NSGA-II 및 FEA 비교 도구 5개와 관련 테스트 2개를 보관합니다. 이동일은 2026-10-30이며, 모든 Python 파일은 원래 바이트와 SHA256을 유지했습니다. 알고리즘, 수치 기준, 테스트 assertion은 수정하지 않았습니다. 이 도구의 과거 결과와 계약은 현행 v3 모델·설계의 승인 근거가 아닙니다.

줄바꿈과 Python 안의 Markdown 문자열 공백도 원본대로 보존합니다. 이 보관 폴더에는 Git의 자동 줄바꿈 변환·공백 스타일 검사를 적용하지 않으며, 파일 해시와 위 검증 기록으로 무결성을 확인합니다.

## 원래 위치와 파일 무결성

아래 원래 경로는 프로젝트 루트 기준입니다. 새 위치는 이 README와 같은 폴더입니다. 각 파일의 독립 원본 백업은 `D:/IPMSM_Archive/pre_rebuild_20261030_161616/working_files/<원래 경로>`에 있으며, 이동 전과 동일한 SHA256을 확인했습니다. [source_map.csv](source_map.csv)에 파일별 정확한 백업 경로와 바이트 크기를 기록했습니다.

| 원래 경로 | 보관 파일명 | SHA256 |
|---|---|---|
| `build_ipmsm_legacy_final_report.py` | `build_ipmsm_legacy_final_report.py` | `9909063f4a73417bb764423f65a6f1f13a1e0a27b074272e76b767f5b4dd45b9` |
| `combine_ipmsm_legacy_final_nsga2.py` | `combine_ipmsm_legacy_final_nsga2.py` | `b76a46968fd02029c2d4089276660eadb8838fdeed0402cc6c7518397165a192` |
| `compare_ipmsm_legacy_final_fea.py` | `compare_ipmsm_legacy_final_fea.py` | `6eee7ac018094d343a3eafc6ec4fbb8435252fb7cf4d3e0e89abfe182febe836` |
| `plot_ipmsm_legacy_final_fea_comparison.py` | `plot_ipmsm_legacy_final_fea_comparison.py` | `69168ba9f2234adb640a580b963055968d48b92855f3805df9237999ce1187c9` |
| `plot_ipmsm_legacy_final_nsga2.py` | `plot_ipmsm_legacy_final_nsga2.py` | `ecc69bb3a0ee4e2554ed3da4cc020af2b95dc96ee10cd0d93ae1a6a5c2d1cdeb` |
| `tests/test_combine_ipmsm_legacy_final_nsga2.py` | `test_combine_ipmsm_legacy_final_nsga2.py` | `5745833f59029f8bc067c80ef372d4112501c85cb41838ea7671a67462bab6cc` |
| `tests/test_compare_ipmsm_legacy_final_fea.py` | `test_compare_ipmsm_legacy_final_fea.py` | `104198e32d1c1fb3e1b316d440b124bcdf0fd7a8960d6a39d3c3da0dbce51026` |

## 실행과 검증

입력 폴더와 CSV 경로는 실행할 때 명시합니다. `build_*`의 `--root`, NSGA-II 그림의 `--combined-dir`, FEA 그림의 `--comparison-dir`/`--predictions`/`--combined-dir`, 비교기의 `--predictions`/`--results`/`--output-dir`, 결합기의 입력 경로/`--output-dir`를 사용합니다. 프로젝트 루트에서 `python legacy/final_design_2026_07/<파일명> --help`로 해당 인자를 확인할 수 있습니다. 도구의 입력 상대경로는 실행한 현재 폴더를 기준으로 해석됩니다.

소스와 테스트를 같은 폴더에 평면 배치해 원래의 plain module import를 유지했습니다. 프로젝트 루트에서 다음 명령으로 보관 테스트를 실행합니다.

```powershell
python -m pytest legacy/final_design_2026_07/test_combine_ipmsm_legacy_final_nsga2.py legacy/final_design_2026_07/test_compare_ipmsm_legacy_final_fea.py --import-mode=prepend -q
```

별도의 새 C 검증 복사본에서 위 테스트 6개와 Python 도구 5개의 `--help`를 검증했습니다. 실제 보고서 생성, 최적화 또는 FEA는 실행하지 않았습니다.

## 과거 root 배치 복원

재현이 필요하면 별도의 복원 작업공간에 과거 Git bundle/tag의 추적 소스를 먼저 복원하고, `source_map.csv`의 `original_relative_path`대로 이 파일들을 복사합니다. 예를 들어 결합기는 복원 작업공간 루트에, 해당 테스트는 `tests/` 아래에 둡니다. 파일마다 SHA256을 다시 비교하고 줄바꿈이나 인코딩을 변환하지 않습니다.

이 7개 파일은 이동 당시 untracked였으므로 기존 production 태그만으로는 그 파일이나 원래 배치를 복원할 수 없습니다. 이 폴더의 파일/경로 명세 또는 위 D 원본 백업을 함께 사용해야 합니다. 다른 과거 도구의 root-relative 모델/adapter/자식 스크립트 의존성은 각 도구의 원래 배치와 별도 자료를 함께 복원해 확인합니다.
