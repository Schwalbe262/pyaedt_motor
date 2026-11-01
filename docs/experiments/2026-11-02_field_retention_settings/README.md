# 필드 보존 요청과 band 저장 설정: Source859 Native0 두 건

원본 공개 `simulate --setup-only` 경로에서 band 세분 144와 0의 저장 설정을 비교했습니다. **두 실행 모두 Analyze 0·FEA 0**이며, 실제 13개 필드 캐시를 확인한 결과가 아닙니다.

## 문제

필드 보존 옵션이 저장된 Native 설정으로 전달되는지, band 세분 선택 외의 입력이 같은지 확인해야 했습니다. 설정 파일에 요청이 남았다는 사실과 해석 후 해당 시간점의 필드 캐시가 실제 존재한다는 사실을 구분합니다.

## 시도

[고정 Source859 코드](https://github.com/Schwalbe262/pyaedt_motor/commit/859318df3f722e91237cb12e1196cb9477a725d5)는 source 674파일·private library 23파일, producer `43067e07…`입니다. M19 G1·1200 rpm·0 A peak·β0°·Ns48·full360의 두 입력은 `band_circle_segments=144`와 `0`만 다릅니다. 원본 public wrapper를 각각 한 번 실행해 저장 후 기존 15초 소유 PID 종료 검사와 client retirement를 거쳤습니다.

3주기·300단계/주기 설정에서 마지막 주기의 **step 600–900, 매 25단계**를 `Every N Steps`로 저장하도록 요청했습니다. 시간 범위는 0.025–0.0375초이며 요청상 예상 시점 수는 13개입니다. 정확한 선택 입력은 [case_inputs.json](case_inputs.json)에 있습니다.

## 확인한 결과

| 요청 band 세분 | 저장된 Band edge 수 | Native PID | wrapper 전체 시간 s | public row 시간 s | wrapper exit |
|---:|---:|---:|---:|---:|---:|
| 144 | 144 | 73824 | 181.469 | 178.333 | 0 |
| 0 | 1 | 49012 | 181.500 | 178.399 | 0 |

두 PID 모두 기존 15초 범위에서 Quit 후 사라졌고 client retirement 및 오류 없는 정리를 확인했습니다. 시간 값은 Native0 설정 생성·저장 경로의 관측값이며 FEA 시간이나 해석 속도 개선값이 아닙니다.

별도 Root 순수 파일 평가에서 저장 setup 35개 속성의 타입과 값, M19 B-H 23점, 깊이 0.0436181 m, winding 24개, report 9개를 확인했습니다. 두 저장본의 Solutions는 모두 비어 있습니다. 두 실제 Spec의 차이도 band 세분뿐이었고, 저장 변수 52개 scalar 문자열·전체 원문 행·30개 행의 미해석 추가 메타데이터는 서로 같았습니다.

초기 순수 검사기 **3회의 실패는 그대로 보존**했습니다. 순서대로 codec 이름 `utf8-sig` 오류, `core_material_fallbacks`의 tuple/list 표현 차이, `VariableProp`의 `oa/sa/ta` 추가 Native 메타데이터 처리 문제였습니다. 실패 로그와 반환 코드를 성공으로 덮어쓰지 않았습니다.

Root의 별도 평가는 요청 대비 13개 형상 문자열에만 동일 단위 접미사를 유지한 **정확한 15유효숫자 표시 규칙**을 적용해 52개 변수를 확인했습니다. 나머지 필드를 일괄 정규화하지 않았고, `oa/sa/ta` 메타데이터는 실행하거나 삭제하지 않고 원문으로 남겼습니다. 이 구체적인 허용 규칙과 원본 해시는 [provenance.json](provenance.json)에 기록했습니다.

## 남은 점

확인 범위는 **필드 저장 요청 설정이 Native 저장본에 남았다는 것**입니다. Analyze와 FEA가 없으므로 시작·끝을 포함한 실제 13개 필드 캐시는 미검증입니다. 해당 캐시를 사용한 후처리 역시 검증하지 않았습니다.

이 자료는 공간 수렴, 평균 토크·손실, 빠른 FEA, 학습 또는 최적설계의 근거가 아닙니다. 별도 [Source17 actual163 해석](../2026-11-02_sdk_solver_baseline/README.md)은 producer `307b1fd8…`의 다른 실행입니다. Source859 Native0의 저장 설정 확인과 섞어 판정하지 않습니다.

[metrics.csv](metrics.csv)는 두 실제 실행의 값만 담습니다. 원시 AEDT·변수 원문·로그 본문은 이 공개 폴더에 복사하지 않았고, 새 Native·API·FEA·원장 작업도 수행하지 않았습니다.
