# 과거 참고자료 보관

2026-10-30에 아래 3개 파일을 바이트 보존 이동했습니다. 파일 본문, 줄바꿈, 인코딩은 수정하지 않았고 SHA256을 원래 D 백업과 다시 비교했습니다.

| 원래 프로젝트 상대경로 | 보관 파일명 | SHA256 |
|---|---|---|
| `FEA_MESH_TIME_PROFILE_OPTIMIZATION_REPORT.md` | `FEA_MESH_TIME_PROFILE_OPTIMIZATION_REPORT.md` | `b60b4e332bc7d36982d09709f32cb57361d5a1170df28e9df206a2439836f57d` |
| `material/record.py` | `record.py` | `34dcac81bd0a58e3a5ee8435c749745e2018720db611c991831133a19b80060b` |
| `material/test.tab` | `test.tab` | `c4d3d683fc723f9a321dfc6973c09e75aaa9cbb8fd035a7dffc8e608851986aa` |

원본 백업은 `D:/IPMSM_Archive/pre_rebuild_20261030_161616/working_files/<원래 상대경로>`에 있습니다. [source_map.csv](source_map.csv)에 각 파일의 정확한 백업 경로와 크기를 기록했습니다.

`FEA_MESH_TIME_PROFILE_OPTIMIZATION_REPORT.md`는 2026년 6월의 과거 메시/시간 프로파일 실행 기록입니다. 현재 production 프로파일의 승인 결과로 사용하지 않습니다.

`record.py`는 2026-06-02에 AEDT 2025.2에서 기록한 material macro입니다. 시작 부분에서 Desktop을 초기화하고 `simulation30` 프로젝트를 활성화합니다. 기록 참고용으로 보존했으며 실행은 검증하지 않았습니다. `test.tab`도 과거 입력 참고자료로 보관합니다. 현행 material CSV 및 물리 소스는 변경하지 않았습니다.

이전 배치를 재현하려면 별도의 복원 작업공간에서 `source_map.csv`의 `original_relative_path`대로 파일을 복사하고 SHA256을 다시 비교합니다. 기존 production 태그에는 당시 untracked였던 이 파일들이 포함되지 않을 수 있으므로 이 경로 명세와 D 원본 백업을 함께 사용합니다. Macro의 프로젝트 이름과 그 밖의 환경 의존성은 원래 기록 그대로 유지했습니다.
