# 이전 테스트의 빈 임시 폴더

2026-07에 남은 프로젝트 루트 임시 폴더 9개를 이곳으로 이동했습니다. 이동 직전 숨김 항목까지 검사했으며 파일은 0개였습니다. 한 폴더의 빈 하위 경로 `terminal/failed_results`도 보존했습니다. 현재 코드에서 해당 임시 이름을 참조하는 경로는 찾지 못했습니다.

보관된 이름:

- `tmpwrjxmyi5`
- `tmpoqpfma4i` (`terminal/failed_results` 포함)
- `tmpzgi5p6gt`
- `tmpyp0r3hw5`
- `tmp8s2px5od`
- `tmpgumk2ud8`
- `tmpsve38vw5`
- `tmpvwl6dxdk`
- `tmpp1wt3qr2`

같은 Y: 볼륨에서 폴더만 이동했고 기존 파일은 수정하지 않았습니다. 복원하려면 필요한 폴더를 프로젝트 루트의 같은 이름으로 이동하면 됩니다. 빈 폴더 구조는 Git에 포함되지 않으므로 다른 checkout에서 필요하면 위 이름과 빈 하위 경로를 생성합니다.

이동 전 계획과 전후 검증은 C: 실행 폴더의 `empty_test_directories_20261031/pre_move_plan.json`, `post_move_verification.json`에 있습니다. 예전 루트 9개가 사라지고, 파일 0개와 하위 폴더 구조가 그대로임을 확인했습니다.
