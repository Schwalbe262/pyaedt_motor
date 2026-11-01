# 원래 SDK 저장·종료 기준 시험

2026-11-01의 실제 클러스터 작업101031은 **저장·입력 검증·소유 Native 종료·Python client 정리까지 통과**했습니다. 계산 없는 시험이므로 FEA 성능·실제 solver 코어 수·새 학습자료 승인을 뜻하지 않습니다.

| 항목 | 실제 결과 |
|---|---|
| 실행 | n116 / dw16 / allocation15116 / completed / exit0 |
| 작업 시간 | 13:31:58–13:32:30 UTC, 32초(작업 시각 해상도) |
| 모델 | Source17 / M19 기준 / 1200rpm / 100A peak / β30° / Ns48 |
| 저장 입력 | 원본 두 개의 전체 물리 입력 비교 통과, Root 재계산과 동일 |
| 종료 | 소유 Native3976629가 원래15초 기준 안에 종료, client 정리 성공, 오류 없음 |
| SDK 환경 | 실제 Native에서 SDK Linux 기본값12개 일치 |
| 실제 명령 | 원래 SDK의 -grpcsrv / -ng, 추가 machine list·batchoptions·logfile 없음 |
| 해석·학습 | Analyze0 / FEA0 / 실제 solver4코어 미검증 / 학습 미승인 |

원래 Source17 factory와 cleanup을 그대로 사용했습니다. SDK에서 정한 기본 환경을 보존하며 직접 기동 어댑터·추가 Release·재연결 모드 변경을 적용하지 않았습니다. 앞선 직접 기동 시험101026–101030의 종료 실패는 그대로 보존합니다. 여러 실행 차이가 함께 있으므로 특정 환경변수 하나가 원인이라고 판단하지 않습니다.

종료 증거의 범위는 양성 식별된 SDK Native 본체의 PID 소멸과 Python client 정리입니다. Native 자체의 exit code나 wrapper·보조 프로세스 전체 종료를 별도로 인증한 것은 아닙니다. 작업 exit0과 Native exit code를 구분합니다. 다음 단계는 같은 경로의 단일 FEA에서 실제4코어 풀이와 출력·저장·종료를 확인하는 것입니다.

[실측 요약](metrics.csv) · [입력·원본 해시·검증 근거](provenance.json)
