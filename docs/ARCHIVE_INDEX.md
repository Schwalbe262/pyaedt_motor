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

현재 클러스터 FEA 소스는 배포 프로젝트 `PYAEDT_MOTOR_REBUILD_V3_20261030`의 고정 커밋 `5914cb3`입니다. 실행 자원·라이선스 정책은 기존 모터 프로젝트 `PYAEDT_MOTOR_IPMSM_V2`를 사용합니다. 진행 중인 작업이 끝나기 전에는 해당 원격 소스 배포를 갱신하지 않습니다.

원격 task ID, 시도 예산, 실험 계획·제출 기록과 수집 결과는 C: 실행 폴더의 `attempt_ledger.jsonl`, `cluster_wave_rb3b`에서 확인합니다. 이 폴더는 완료 후 D:에 보관합니다. `tools/collect_cluster_wave.py`는 이 제출 기록을 읽어 결과와 실제 메시 요소 수를 수집하며, 작업을 새로 제출하거나 재시도하지 않습니다.
