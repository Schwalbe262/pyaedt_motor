# 공개 CAD 생성과 별도 CLI 풀이 시험

## 개선 목적과 현재 범위

Linux 공개 API의 CPU 요청과 실제 행렬 코어 수가 달라, 문서화된 세션 전용 `-batchoptions` 경로를 검증하는 선택형 시험 모듈을 추가했습니다. 기본 `simulate`/`run_one_case`의 Analyze 동작은 유지합니다. 현재 모듈은 진단용이며 학습 데이터나 생산 프로파일을 승인하지 않습니다.

`src/pyaedt_motor/public_batch_pilot.py`는 다음 순서로 실행합니다.

1. 공개 생성기의 `--setup-only --prepare-batch-reports --keep-projects`에 해당하는 경로로 새 CAD와 보고서를 생성합니다.
2. 빌드 Native의 실제 소유권·종료·Python client 정리와 Analyze 미실행을 확인합니다.
3. 실제 예약이 있는 solve 모드에서만 별도 소유권 헬퍼로 CLI를 한 번 실행합니다. 빌드 PID와 풀이 PID는 구분합니다.
4. 알려진 정상 종료 뒤에 원시 CSV와 Native 프로파일을 읽어 10개 출력·시간 격자·코어 사용을 검사합니다.

불확실 종료에서는 Native에 다시 접속하거나 재시도하지 않습니다. CLI 결과를 기존 SDK Analyze-return 기록으로 위장하지 않습니다. 공유 레지스트리 변경과 TDM/MDD 설정을 병렬 처리의 우회책으로 사용하지 않습니다.

## 입력과 실행

Linux/Ansys2025.2·검토한 23파일 라이브러리·full360/Sym1·4CPU·새 출력 폴더를 대상으로 합니다. 계약에는 소스·라이브러리·시스템 재료·설치 바인딩·케이스의 크기/SHA가 필요합니다. 실제 호스트와 Slurm 노드를 각각 확인합니다. solve 예약은 승인 여부·시도 번호·케이스 SHA·예약 참조를 제외한 계약 전체 SHA에 결합합니다.

설치된 벤더 실행기의 심볼릭 링크는 V6 설치 검증기가 링크 연결과 대상 위치를 확인한 후 실제 파일의 크기/SHA를 검사합니다. 종료 후에도 전체 설치 검증을 반복합니다. 소스·케이스·계약 파일의 심볼릭 링크는 허용하지 않습니다.

```text
python -I -B SOURCE/src/pyaedt_motor/public_batch_pilot.py --contract ABS.json --contract-sha256 SHA --output-root NEW_ABS
```

기본은 입력 검증만 하는 dry 모드입니다. 별도 `--execute`가 있어야 계약의 `setup_only` 또는 `solve`를 수행합니다. 설정 전용 모드는 FEA 예약과 풀이를 만들지 않습니다. 초기 진단은 보정 ID를 비워 두고 숫자 좌표 원점의 근거를 별도 보관합니다. 과거 생성기의 보정 ID를 새 생성기 보정으로 재사용하지 않습니다.

## 결과 해석과 남은 작업

원시 과학 트리·전체 ReportSetup·실제 보고서/관리자·재료·변수의 변경을 따로 기록합니다. 버전 ID·형상 캐시·표면 속성을 자동으로 무시하지 않습니다. 저장본 차이가 미해결이면 원시 지표를 보존하고 `solved_saved_closure_unproven`으로 구분합니다. 이 상태는 수치 정확도 실패나 생산 합격과 같지 않습니다.

`public_cli_retained_inputs_v1`은 실행 전후 검토에 필요한 입력을 추가 보존합니다. 빌드 세션 종료 후 미해석 AEDT를 `prepared_unsolved.aedt`로 복사하고 그 복사본에서 설정을 파싱합니다. 설치 파일·링크의 전체 관측을 전후 JSON으로 남기며, 실제 `options.txt`의 크기/SHA와 미해석 원본·설치 관측을 CLI 실행 의도에 연결합니다. 실행 직전과 종료 후 같은 기록을 재검사합니다. 이 파일 보존은 추가 Native 세션이나 풀이를 만들지 않습니다.

실제 설정 전용 실행과 같은 조건의 3/6주기 풀이를 수행했습니다. [실제 비교](experiments/2026-11-01_public_cycles/README.md)에서 기존 10개 민감도 기준은 통과했지만, 저장 상태 동등성·여러 형상과 운전점의 공간/시간 수렴 및 정식 데이터 수집·승인 연결은 남아 있습니다. 기존 두 작업의 실패 상태와 학습 미승인을 유지합니다. 효율과 출력의 정의는 [출력 범위](MOTOR_OUTPUT_DEFINITIONS.md)를 따릅니다.
