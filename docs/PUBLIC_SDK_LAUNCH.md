# 진단용 private SDK 시작 경로

현재 지원 범위는 Linux Python 3.11, `dw16`/UID1641, `n116`, AEDT25.2,
진단용 단일 case·worker1·요청4코어·standalone·non-graphical 실행이다.
기존 저장 seed 진단과 별개로, 이 공개 진입점의 **새 CAD 생성 실행은 아직
Native0/FEA 통합 검증을 마치지 않았다**. 대량 생산·학습 승인으로 사용하지 않는다.

옵션을 생략하면 기존 SDK 실행 경로를 사용한다. 명시적으로 선택한 경우에는
`simulate` 또는 cluster의 `subprocess_run.py`가 격리된 `-I -B` Python child로
프로필과 SHA를 함께 전달한다. SDK import와 설정은 그 child에서 수행한다.

## SDK와 실행 프로필

이 경로는 **수정된 private PyAEDT0.22 사본**을 사용한다. SDK 시작부의 작은
launch-only backport이며, 공개0.22에 있는 공식 시작 인자 API라고 주장하지 않는다.
전체 SDK를 이 저장소에 vendor하거나 원래 설치를 덮어쓰지 않는다. 사전에 검증한
외부 private 사본과 실제 `SDK_IDENTITY.json`/import proof가 필요하다.

프로필에는 source commit과 필요한 소스 해시, private SDK 정체성, 원래/private
SDK1189파일, 기존 명시적 library23, 설치11파일의 바인딩이 필요하다. 프로필은
CLI가 지정한 SHA와 같은 바이트에서 검증·파싱하며, 이후에도 최초 pin을 유지한다.
별도 참조도 검증한 동일 바이트만 반환한다. 누락·변경·혼합 import는 거부한다.

검토용 템플릿은 `execution_enabled=false`이며 source commit/library 위치 등의
미결정 값이 있다. 이 문서는 그 값을 채우거나 양성 권한을 발행하지 않는다.
프로필 해시가 FEA 예산·실행 영수증·과학적 승인 역할을 대신하지도 않는다.

시작 직전 현재 자신의 Slurm job/step을 제한된 읽기 전용 명령으로 확인한다.
요청4코어가 실제 job>=64CPU/step>=4CPU 안에 있어야 하고 task 요청은4,
node/UID도 일치해야 한다. `SLURM_*`를 고쳐 할당량을 위조하지 않는다.
검증한 `ANSYSEM_TASKS_PER_NODE=4`와 고정된 launch 인자만 이 경로에 추가한다.
소유 Native의 argv/선택된 환경값을 확인한 뒤 기존 CAD 작업을 진행한다.

## 진입점

아래는 검토 후 사용할 형태이며, 비활성 템플릿으로는 Native를 시작하지 않는다.
먼저 기존 절차의 예산·자원·소스·단일 case 검토를 완료해야 한다.

```sh
python -m pyaedt_motor simulate \
  --sdk-launch-profile /ABS/reviewed_profile.json \
  --sdk-launch-profile-sha256 PROFILE_SHA256 \
  --cases /ABS/one_case.csv --workers 1 --cores 4 \
  --aedt-backend standalone --non-graphical --keep-projects \
  --setup-only --prepare-batch-reports
```

Cluster도 같은 두 옵션을 받는다. 기존 diagnostic plan/source/ledger 인자와
제출 전 검토를 유지하고 `--max-workers-per-node 12` 및 전체 활성 cap<=300을
지킨다. child는 여전히 한 case·worker1이다. 이 문서는 `--execute`나 FEA 실행을
승인하지 않는다. 기존 원장·중복 방지·불명확한 제출 상태 처리도 그대로다.

## 실제 코어와 종료 판정

요청 인자·Slurm 할당·ACF의 `NumCores=4`는 실제 풀이 코어 수의 증거가 아니다.
옵트인 해석은 원래 exactTrue 반환, 검증된 소유 identity, solver uncertainty 해소,
원래15초 Quit 종료와 client retirement가 모두 확인된 뒤에만 원시 파일을 읽는다.
종료나 소유권이 불명확하면 Native profile/ACF 탐색·읽기를 하지 않는다.

기존 `solver_profile.audit_solver_profile`로 다음을 모두 확인한다.

- 완성된 saved-native 프로필과 Normal Completion.
- machine 코어4 및 누락 없이 모든 matrix 풀이 코어4.
- 해당 프로젝트 ACF의 요청 코어4와 원시 bytes/SHA.

공유 SDK의 `misc/pyaedt_local_config.acf` 템플릿을 실제 풀이 증거로 쓰지 않는다.
읽는 ACF는 `<project>/<simulation>.pyaedt/<design>/pyaedt_config.acf`다.
프로필은 해당 `<simulation>.aedtresults/<design>.results`의 정확히 한 파일만
허용하며, 프로젝트 밖/심볼릭링크/누락·복수 파일은 거부한다.
프로필<=4MiB, ACF<=256KiB이며 형식·무결성·코어가 불명확하면 실패로 남긴다.

순수 검사 결과와 입력 해시는 `profile4_audit.json`에 저장하고 기존
`after.json` artifact가 이를 가리킨다. 이전 실패 이유는 덮어쓰지 않는다.
설정 전용은 `not_requested_setup_only`이며 실제4코어를 주장하지 않는다.
검증 실패가 factory 뒤에 발생해도 정리에는 정확한 owner Desktop을 전달한다.
추가 factory/강제 kill/자동 재시도/불명확한 solver에 대한 재접속은 추가하지 않는다.

원래 SDK의 Linux12 기본 환경 적용은 child의 정상 동작이다. 이를 전부 원복했다고
주장하지 않는다. 추가 Ansys resource 입력과 시작 attribute는 Python 범위에서
복원하고 Slurm whitelist 불변을 확인한다. 추가 전역 HPC setter는 호출하지 않는다.

Normal Completion이나 실제4코어 통과는 물리10출력, mesh/time 수렴, 좌표계·보정,
학습 profile 또는 모델 정확도 승인과 별개다. 새 소스/producer의 provenance를
그대로 남기고 기존 과학·학습 gate를 별도로 적용해야 한다.

## 저장소 회귀 검사

```sh
python -m unittest discover -s tests -p 'test_ipmsm_sdk_launch*.py' -v
```

검사는 현재 저장소 소스와 임시 파일만 사용한다. 실제 SDK·Ansys 설치나 네트워크가
필요 없고, Native/OS 경계는 명시적 mock이다. 원래 RED 재현과 과거 C 봉인은 외부
연구 archive의 `private_SDK_public_launch_integration_candidate_*_v2/_v3/_v4`
검토 기록에 보존된다. 이 과거 archive는 위 검사의 의존성이 아니다.
