# 마지막 전기 주기의 선택적 자계 저장

공극 자계로 독립 토크를 계산할 때 서로 다른 전기 위상의 자료가 필요합니다. 모든 시점을 저장하는 대신 마지막 완전 주기만 일정한 solver step 간격으로 저장하도록 선택할 수 있습니다. 이 기능은 향후 고정할 새 소스용이며, 현재 클러스터의 불변 Source17에는 포함되지 않습니다.

| CSV 입력 | 의미 | 3주기·주기당300 step의 예 |
|---|---|---:|
| save_fields_start_step | 마지막 주기의 첫 solver step | 600 |
| save_fields_stop_step | 마지막 주기의 끝 solver step | 900 |
| save_fields_every_n_steps | 저장 간격(주기 수가 아닌 solver step 수) | 25 |

세 입력을 모두 생략하면 기존 SaveFieldsType=None과 기본 설정 fingerprint를 유지합니다. 입력할 때는 세 값 모두 정수여야 하며, 시작·끝은 마지막 전기 주기를 정확히 덮고 간격은 주기당 step 수를 나누어야 합니다. 일부만 지정하거나 bool·소수·범위 밖 step·나누어떨어지지 않는 간격을 주면 해석 전에 거부합니다. 기록된 input_save_fields_* 열도 다시 읽을 수 있고, 서로 다른 별칭 값은 거부합니다.

8극·1200rpm의 전기 주기는12.5ms입니다. 위 예는25–37.5ms에서13개 endpoint를 요청하므로30° 간격의12개 고유 위상과 시작·끝의 같은 위상을 갖습니다. 시간 범위는 Native에 실제 명령하는 RPM 표기에서 계산합니다. 시간·공간 수렴을 보장하는 표본 수로 인증한 것은 아닙니다.

PyAEDT0.22의 SetupMaxwell.set_save_fields Every N Steps 분기와 같은 속성을 사용합니다. 선택 기능을 켜면 SDK 자동 갱신을 먼저 끄고 모든 설정을 구성한 뒤 명시적으로 한 번 갱신합니다. 자동 갱신을 재현한 회귀검사는 수정 전12회에서 수정 후 완성된 설정1회로 바뀌었습니다. 이는 단위검사의 관측이며 실제 Native 실행시간 측정은 아닙니다.

현재 검증은 기본32개 속성 보존, CSV→spec→기록→설정 전달, 잘못된 입력 거부와 SDK 속성 대조까지입니다. **실제 Native의 끝점 포함 여부와 자계 cache 생성은 아직 미검증입니다.** 실제 비교 전 새 소스·producer로 두 회전 경계 조건을 함께 다시 생성하고, 저장 설정·재료·깊이·시간·경계·프로세스 정리를 확인해야 합니다. 기존 Source17의 자료·교정·모델 승인을 옮기지 않습니다.

구현: [설정·검증](../module/ipmsm_ppt_setup.py) · [CSV·fingerprint](../run_ipmsm_batch.py) · [회귀검사](../tests/test_ipmsm_field_retention.py)
