# 과거 시뮬레이션 데이터 위치

프로젝트의 `legacy/simulation/`은 Git에 포함하지 않는 로컬 보관 데이터입니다. 현재 연구 결과와 섞지 않도록 이전 루트 `simulation/`에서 같은 Y: 볼륨 안으로 이동했습니다.

| 위치 | 파일 수 | 크기 | 역할 |
|---|---:|---:|---|
| `Y:/git/pyaedt_motor/legacy/simulation` | 40,311 | 3,009,541,478 bytes | Y:에 남아 있던 원본 |
| `D:/IPMSM_Archive/y5` | 73,052 | 5,226,020,510 bytes | 앞서 D:로 이동한 원본 |
| `D:/IPMSM_Archive/s0` | 113,363 | 8,235,561,988 bytes | 전체 독립 백업 |

위 두 원본 경로의 합집합은 전체 백업의 파일별 SHA와 일치합니다. `legacy/simulation`만으로는 전체 데이터가 아닙니다. 전체 복원이 필요하면 `s0/archive_manifest.csv`와 `docs/ARCHIVE_INDEX.md`의 대응표를 기준으로 빈 목적지에 복사하고 파일 SHA를 확인합니다.

이동 전후 대응은 `simulation/<상대 경로>` → `legacy/simulation/<같은 상대 경로>`입니다. 데이터 파일 내용은 변경하지 않았습니다. 이동 후 전 40,311파일의 크기와 SHA를 새로 검사하여 누락·추가·불일치·읽기 오류가 모두 0건임을 확인했습니다.

검증 증명: `C:/Users/peets/NEC/pyaedt_motor_runtime/rebuild_20261030/simulation_directory_relocation_20261031/post_move_verification_summary.json` (SHA256 `7834f2417f34961274823c56c05f04d1ce2520597d2561c4d5f48518fa2ae236`). 현재 해석의 입력·결과 위치는 `HANDOFF_CURRENT.md`를 참고합니다.
