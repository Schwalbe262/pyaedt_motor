# 실행본과 보관 위치

현재 코드는 `Y:\git\pyaedt_motor`, 새 해석·수집 작업은 `C:\Users\peets\NEC\pyaedt_motor_runtime\rebuild_20261030`에서 관리합니다.

| 위치 | 내용 |
|---|---|
| `D:\IPMSM_Archive\pre_rebuild_20261030_161616` | 정리 전 전체 Git 참조 bundle, 기존 수정 patch, 수정·미추적 파일의 검증된 사본 |
| `D:\IPMSM_Archive\retired\index.json` | C:에서 D:로 옮긴 과거 실행본 20개의 원래 경로·보관 경로·검증 백업 대응표 |
| `D:\IPMSM_Archive\inactive_clones_20261030` | 과거 실행본별 파일 해시 manifest와 Git bundle. 긴 경로 2개는 아래 위치에 보관 |
| `D:\IPMSM_Archive\20261030\clone_02`, `clone_05` | Windows 경로 길이 제한 때문에 짧은 경로를 사용한 실행본 백업 |
| `D:\IPMSM_Archive\scheduled_tasks_20261030` | 이전 모터 예약 작업 25개의 XML 정의와 정리 전 상태 |

20개 실행본의 7,250개 파일은 백업을 검증한 뒤 이동했고, 이동 결과도 원래 manifest와 다시 대조했습니다. 원래 작업 경로가 필요한 과거 계약은 보관 위치에서 바로 실행하지 말고 해당 소스 버전과 원래 경로를 복원해야 합니다. `.git` 파일을 사용하는 작업 트리 하나는 이동 후 Git 연결을 복구했습니다.

기존 데이터와 읽기 전용 대시보드가 사용하는 `C:\Users\peets\NEC\pyaedt_motor_blob_lf_325`는 유지합니다. 켜져 있던 과거 예약 작업 7개는 비활성화했습니다. 실행 중이던 대시보드 프로세스는 유지하며, 새 물리 계약에 대한 승인을 과거 자동화 계약으로 대신하지 않습니다.

현재 M19 기준 모델의 클러스터 FEA 소스는 `PYAEDT_MOTOR_REBUILD_M19_43006688`의 고정 커밋 `4300668`입니다. 이전 source4~7은 각 실험의 재현용으로 보존합니다. 실행 자원·라이선스 정책은 기존 모터 프로젝트 `PYAEDT_MOTOR_IPMSM_V2`를 사용하며, 진행 중인 작업의 원격 소스를 갱신하지 않습니다. 최신 상태는 `HANDOFF_CURRENT.md`에서 확인합니다.

원격 task ID와 시도 예산은 C: 실행 폴더의 `attempt_ledger.jsonl`, 현재 M19 영점 해석은 `m19_zero2_43006688`, 이전 PNF 실험은 `cluster_wave_rb3b` 등 각 실행 폴더에서 확인합니다. 완료된 실행은 D:에 보관합니다. 수집 도구는 결과와 실제 메시 요소 수를 읽으며, 수집만으로 작업을 새로 제출하거나 재시도하지 않습니다.


## 2026-10-30 추가 정리

Y:의 과거 수집·모델·PPT 추출·md 폴더는 각각 D:의 `y0`~`y4`로 이동했고, 이동 뒤 742파일(61,925,369 bytes)의 해시를 검증했습니다. 검증 백업은 순서대로 `yc0`, `ym0`, `yp0`, `yp1`, `yd0`입니다. 시뮬레이션 113,363파일(8,235,561,988 bytes)은 `D:\IPMSM_Archive\s0`에 전체 해시 검증된 독립 백업이 있습니다. 원본은 D:의 `y5` 73,052파일과 프로젝트 `legacy/simulation`의 40,311파일로 나누어 보관합니다. 아래 이동 증명과 `legacy/LOCAL_DATA.md`를 참고합니다.

남은 Git 작업트리 3개는 `D:\IPMSM_Archive\wb0`에 일반 파일 994개와 Git 관리파일 23개를 백업했습니다. 모든 ref bundle도 별도 복원·fsck를 통과했습니다. C: native 작업트리는 `w0`으로 이동·repair·일반 파일 해시 검증을 마쳤고, Y:의 두 작업트리도 `w1`, `w2`로 이동·repair·전체 일반 파일 해시 검증을 완료했습니다. `.git` 경로 정규화의 전후 기록도 보존합니다. 브랜치·main 통합은 별도 단계이며 현재 이 백업을 완료 근거로 사용합니다.

현재 상세 이동·검증 상태는 C: 실행 폴더의 `retired_y_move_plan.json`, `retired_y_verified.json`, `leftover_worktree_backup_index.json`에서 확인합니다. 완료된 이전 연구 버전은 Git tag와 bundle로 복원할 수 있도록 정리합니다.

2026-10-31 기존 simulation 원본의 분할 보관을 확인했습니다. `D:\IPMSM_Archive\y5`의 73,052파일과 Y:의 40,311파일 합집합이 전체 백업과 SHA 일치했습니다. Y: 잔여 폴더는 같은 볼륨에서 `simulation` → `legacy/simulation`으로 이동했습니다. 이동 후 40,311파일(3,009,541,478 bytes)을 전부 새로 해시하여 누락·추가·크기·SHA 불일치·읽기 오류 0건을 확인했습니다. 프로젝트 루트의 이전 `simulation` 경로는 없어졌으며, D:의 백업과 분할 원본은 유지합니다. 증명은 C: 실행 폴더의 `simulation_directory_relocation_20261031/post_move_verification_summary.json`입니다. 기존 smoke 원본 7,350파일(19,385,885,523 bytes)은 `D:\IPMSM_Archive\sm1`으로 이동하고 전체 SHA를 재검증했습니다. 독립 백업은 `sm0`에 있으며 Y:의 기존 smoke 폴더는 정리됐습니다.


## main과 과거 브랜치

검증한 연구 코드를 main에 통합하고, 과거 브랜치 6개는 원래 HEAD를 가리키는 원격 annotated tag를 확인한 뒤 정리했습니다. 태그는 `archive/20261030/chore/...`, `archive/20261030/release/...`, `archive/20261030/fix/...`이며 이전 main은 `archive/20261030/main-before-rebuild`입니다. 현재 local·origin 작업 브랜치는 main과 research/ipmsm-rebuild-20261030입니다. D의 3개 작업트리는 원래 HEAD에서 detached 상태로 보관했습니다. 상세 전후 ref는 C 실행 폴더의 `git_consolidation_result.json`을 참고합니다.

과거 untracked 분석·그림 도구 5개와 테스트 2개는 `legacy/final_design_2026_07`에 모았습니다. `source_map.csv`와 README에 원래 경로·SHA·복원 방법이 있습니다. `.gitattributes`도 해당 보관 파일의 원래 바이트를 유지하도록 설정했습니다. 원래 수정돼 있던 adaptive 소스·테스트 2개는 작업 파일에서 변경하지 않았습니다.

이전 replay 분석·계획 도구 4개와 일반 테스트 4개는 `legacy/replay_analysis`에 모았습니다. 원본 8파일(55,649 bytes)은 `D:\IPMSM_Archive\ra0`에 먼저 백업·검증했고, 이동 후 SHA도 대조했습니다. CRLF·코드·테스트 기준을 그대로 보존하며, 보관 폴더 README에 별도 19개 테스트 실행 명령을 남겼습니다. 과거 권위 계약에 묶인 다른 루트 소스·테스트는 유지했습니다.

원격 source 프로젝트4는 기존5914cb3 수렴 실험을 위해 유지합니다. 이전 source 프로젝트5 `PYAEDT_MOTOR_REBUILD_V3B_8BB5216`은8bb5216에 고정하여 보존합니다. 현재 M19 기준 해석은 위 source8의 고정4300668을 사용합니다. 프로젝트5의 max_active_tasks=0은 source 관리 전용이라는 뜻입니다. 실제 해석은 기존 runtime/license 프로젝트2와 승인된 cap300을 사용합니다.


2026-10-31 루트의 이전 테스트 임시 폴더9개를 `legacy/empty_test_directories_2026_07`로 같은 볼륨에서 이동했습니다. 숨김 항목 포함 파일0개·빈 하위 폴더2개이며, 기존 루트 부재와 하위 구조 보존을 확인했습니다. 이름과 복원 방법은 해당 README, 전후 증거는 C 실행 폴더의 `empty_test_directories_20261031`에 있습니다.


2026-10-31 새 검증 후보source9 `PYAEDT_MOTOR_REBUILD_M19_C53D3BA`를 추가했습니다. commit `c53d3ba3101e84e04fc01de78d250db698bdd2e0`에 고정하고 `auto_pull=False`·`max_active_tasks=0`으로 별도 checkout 배포를 시작했습니다. 실제 Native 설정 확인 전의 후보이며 기존source8을 대체하지 않습니다. 새 producer의 영점 보정·품질 검증을 거쳐 사용 범위를 결정합니다. 실행·라이선스project2와 FEA cap300은 유지합니다.
