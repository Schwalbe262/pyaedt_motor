# 원본 SDK 단일 해석 기준점: actual163

M19·G1의 한 진단점에서 해석부터 SDK 소유 PID 정리까지의 경로와 실제 코어 수를 확인했습니다. 최종 task는 **4코어 자원 검증 실패(exit 1)** 로 보존합니다.

## 문제

동일한 과학 입력을 원본 SDK Analyze 경로로 풀었을 때 저장·export·정리를 완료하는지, `cores=4` 요청이 실제 4코어 실행으로 이어지는지 확인할 필요가 있었습니다. 이 시험은 기존 CLI 종료 대기의 보편적 원인을 확정하는 실험이 아닙니다.

## 시도

[Source17의 고정 코드](https://github.com/Schwalbe262/pyaedt_motor/commit/98ed58a824509ca517280017a49059b35aa56f0d)와 producer `307b1fd8…`를 사용한 budget attempt 163, task 101033입니다. 기존 소유 Desktop factory, `app.analyze(setup='PPT_Transient', cores=4, use_auto_settings=False)`, 기존 Save/Export와 15초 소유 PID 종료 검사를 재사용했습니다. 앞선 actual162/task101032의 시작 전 실패는 별도 실패로 유지했습니다.

입력은 M19 참조 모델·G1·1200 rpm·100 A peak·β30°·8극/12슬롯·상 직렬 권수 Ns48·full360입니다. 3 전기주기, 300단계/주기, 총 900단계/닫힌 901표본, 시간 간격 4.1666666666666672e-05초, 종료시간 0.037500000000000006초를 사용했습니다. Native 깊이 읽기는 0.0436181 m였습니다. band 원주 세분 수는 144이며 Ns48 권수와 별개입니다. 정확한 선언 입력과 깊이 읽기는 [case_inputs.json](case_inputs.json)에 보존했습니다.

## 확인한 결과

SDK Analyze가 정확한 Boolean `True`를 반환했고 저장·9개 보고서 export·기존 15초 범위의 소유 Native main PID 4021407 종료·Python client retirement를 확인했습니다. 정리 오류는 없었습니다. 이는 SDK 소유 main PID에 대한 증거이며 Native exit code 0이나 wrapper/auxiliary 종료 인증을 뜻하지 않습니다.

SDK Analyze 로그 시간은 **15분 23초(923초)**, task 전체 시간은 UTC 2026-11-01 14:31:55–14:47:56의 **961초**입니다. SDK 로그 시간은 순수 solver-only 시간으로 재정의하지 않으며 서로 다른 실행 경로의 속도 개선율을 계산하지 않습니다. 이 문서의 날짜는 한국시간 2026-11-02 기록일입니다.

HPC after 제어 파일에는 `ActiveDSO=pyaedt_config`, ACF에는 `NumCores=4`, `NumEngines=1`, `UseAutoSettings=False`가 기록됐습니다. 그러나 실제 Native profile은 machine 1개 기록에서 **1코어**, matrix **901개 기록 모두 1코어**였습니다. 설정 기록의 4와 실제 실행의 1을 구분하며, 4코어 승인으로 대체하지 않습니다. 이 불일치의 원인은 아직 확정하지 않았습니다.

기존 수치 물리 검사, 저장 입력 의미 비교, v252 정상 완료 표식은 통과했습니다. 누락 출력과 물리 검사 이슈는 없었습니다. 원본 9개 SDK CSV에서 Source17의 기존 함수로 아래 10개 출력을 독립 재계산해 worker 값과 상대 1e-10/절대 1e-12 허용차 안에서 일치시켰습니다. 세부 수치는 [metrics.csv](metrics.csv)의 원래 계약 필드명과 전체 정밀도를 기준으로 읽습니다.

| 출력: 최종 전기주기 | 값 | 단위 |
|---|---:|---|
| 평균 전자기 토크 | 20.4830284257 | Nm |
| 최대 전자기 토크 | 25.1035410735 | Nm |
| 평균 철손 | 25.1100625793 | W |
| 평균 고체손실 | 8.36602223923 | W |
| 평균 총손실 | 225.817820737 | W |
| 기존 손실 기반 추정 효율 | 91.9344760441 | % |
| 평균 Ld | 0.00067518243916 | H |
| 평균 Lq | 0.00066508397521 | H |
| 상전압 절댓값 peak | 27.1461652608 | V |
| 전압 span peak | 48.3849000005 | V |

전자기 토크의 `Tω`는 2573.97326502 W(약 2.574 kW), 복원 입력전력은 2799.95356631 W, 기존 전력수지 잔차는 **+0.162480553936 W / +0.00580297315967%**였습니다. 이 전력과 효율의 범위는 [출력 정의](../../MOTOR_OUTPUT_DEFINITIONS.md)를 따릅니다. 순축출력이나 인버터·기계손실을 포함한 시스템 효율로 확장하지 않습니다.

## 남은 점

최종 worker 상태는 `solved_postprocess_or_quality_rejected`, 순수 후처리 상태는 `solved_resource_evidence_incomplete`입니다. 실제 4코어 조건이 충족되지 않아 task의 실패 상태를 유지합니다. 지원되는 HPC 설정이 실제 profile의 4코어로 이어지는 경로는 별도로 검증해야 합니다.

이 자료는 평균 토크 약 20.48 Nm인 M19 G1 진단점입니다. Ns40 최적설계나 7.5 kW 목표 달성 결과가 아니며 β calibration ID는 공란입니다. 기존 정상상태·물리 검사를 통과했다는 사실을 재료 정확도, 공간·시간의 완전 수렴, 정확도 인증 또는 학습·생산 admission으로 바꾸지 않습니다.

원본 봉인 377파일 중 게시 항목 88개/3단계 SHA 파일 266개를 두 차례 seal로 확인했습니다. 관련 소스·입력·보고서·profile·정리 증거의 해시는 [provenance.json](provenance.json)에 기록했습니다. 원시 CSV·AEDT·profile 본문은 이 공개 폴더에 복사하지 않았습니다.
