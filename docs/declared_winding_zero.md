# 새 권선·형상의 전기각 영점

36턴 등 새 권선은 완전한 선언 명세와 실제 두 속도 무부하 결과로 영점을 생성합니다. 이번 기능 검증은 명시적 INERT 자료이며 실제 설계 합격을 뜻하지 않습니다.

새 경로는 8극·12슬롯·상당 직렬 코일 4개·병렬 분기 1개에서 Ns=4×코일 턴수, 도체 면적·100°C 저항·점적률·실제 코일 턴수·원시 파형·원래 소유 프로세스 종료를 함께 검사합니다. 32/36/40/48턴을 검사했으며 기존 40턴/기본 48턴 경로는 유지했습니다.

## 설계 입력 초안

configs/candidates/20261102_g36_w36의 G36은 형상과 도체 면적을 수정한 우선 후보, W36은 기존 형상에서 구리량을 맞춘 대조 후보입니다. 둘 다 36턴/코일9턴이며 도체 면적은 각각20.0/17.022941mm², 계산 점적률은 약32.712/26.185%입니다. 성능·전압 합격은 미확인입니다.

## 사용

```text
python -I -B src/pyaedt_motor/declared_winding_study.py generate --spec configs/candidates/20261102_g36_w36/G36_complete_spec.json --geometry configs/candidates/20261102_g36_w36/G36_geometry17.json --stack-mm 70 --output NEW_G36_HELD
```

지정한 형상·적층을 보존한 두 행의 HELD 입력만 생성하며 해석·예약·영점 발행은 하지 않습니다. 실제 실행 전 소스·노드·단계별 중복 없는 case ID와 수치 설정을 확정하고 CSV 전체를 봉인합니다. 기본 reference_ultra를 생산 수렴 인증으로 간주하지 않습니다.

새 소스의 FS·Native0·FEA smoke와1200/5000rpm 원본을 모은 후 완전한 evidence plan으로 분석합니다.

```text
python -I -B src/pyaedt_motor/declared_winding_study.py analyze --spec configs/candidates/20261102_g36_w36/G36_complete_spec.json --evidence-plan REAL_COMPLETE_PLAN.json --evidence-sha256 SHA256 --output NEW_ZERO.json
```

study schema는 ipmsm_declared_winding_two_speed_study_v1, outer evidence schema는 ipmsm_declared_winding_production_zero_evidence_v1입니다. 명시 protocol_id, 완전한 spec, 수정하지 않은 실제 SDK input_case, 원시9보고서/profile/ACF refs와 진단 예약이 필요합니다. 실제 Linux 원본 SDK 증거와 actual_observation_eligible을 검사합니다. INERT는 별도 테스트 식별자로만 처리합니다.

원래40 생성 결과와 기본48 CSV의 바이트 정합, 기존 연구/보정/좌표계 원문 보존을 확인했습니다. 구현 SHA에 결합된 새 manifest ID는 새 코드에 맞게 생성됩니다. 기존8bc 영점을 새 권선에 재사용하지 않습니다. 최종 통합 검사34개 통과와 실제 새 권선의 정확도·7.5kW/190V·모델 학습 합격은 구분합니다.
