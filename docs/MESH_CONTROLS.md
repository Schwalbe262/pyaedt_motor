# 공간 메시 제어

`mesh_elements`는 추가 요소 개수의 제한입니다. 최종 삼각형 수나 최대 변 길이를 뜻하지 않습니다. 길이와 개수 제한을 함께 쓰면 먼저 도달한 조건에서 정교화가 멈출 수 있습니다. [Ansys v252 설명](https://ansyshelp.ansys.com/public/Views/Secured/Electronics/v252/en/Subsystems/Maxwell/Content/Mesh/LengthBasedMeshRefinement.htm)

| 설정 | 개수 제한 | 길이 제한 |
|---|---|---|
| 모드 생략 | 기존 `mesh_elements` 적용 | 지정한 영역에만 적용 |
| `length_only` | 비활성화 | 해당 영역의 양수 mm 값 필수 |

지원 영역은 `magnet`, `rotor`, `stator`, `winding`, `band`입니다. 선택 프로파일의 `settings`에 다음 항목을 추가할 수 있습니다. 아래 값은 설정 예시이며 승인된 생산 프로파일이 아닙니다.

```json
{
  "mesh_refinement_modes": {"band": "length_only"},
  "mesh_maximum_lengths_mm": {"band": 0.5}
}
```

케이스 CSV에서는 `mesh_band_mode=length_only`, `mesh_band_max_length_mm=0.5`로 지정합니다. `input_` 접두사가 있는 결과 행에서도 같은 설정을 재구성합니다. 모드·영역 이름 오류, 길이 누락, 0·음수·비유한 값과 충돌하는 별칭은 거부합니다.

모드는 프로파일 선택 → DOE → 설정 지문 → 최적 후보의 직접 FEA 계획까지 전달됩니다. 기본 모드를 생략한 기존 DOE와 설정 지문은 유지합니다. 결과에는 영역별 모드·길이와 전체 모드 매핑을 기록합니다.

먼저 설정 전용 실행으로 실제 적용을 확인합니다.

```powershell
python -m pyaedt_motor simulate --cases cases.csv --setup-only --max-cases 1
```

명시적 모드를 사용하면 검증 전후 Native Mesh 객체에서 할당 객체·활성 상태·내부 정교화·개수/길이 제한·mm 길이를 확인합니다. 불일치는 해석 전에 중단하고 진단 파일에 남깁니다. `RestrictElem=False`일 때 저장된 `NumMaxElem=1000`은 비활성 자리값입니다. Native 화면에서 숨겨진 자리값은 저장 AEDT의 독립 검사로 확인합니다.

2026-10-31 Windows/Ansys 2025.2에서 G1 설정 전용 실행을 완료했습니다. 다섯 영역의 검증 전후 Native 값과 저장 AEDT의 객체 ID·제한 플래그·단위·자리값을 대조했고 Analyze 호출은 0회였습니다. 근거는 C: 실행 폴더의 `mesh_length_only_windows_metadata_20261031/independent_native_assessment.json`(SHA `00405a78…`)입니다.

실제 공간 수렴은 같은 형상·재료·전류 파형·시간 간격에서 길이를 단계적으로 줄여 확인합니다. 전역 삼각형 수와 지역 통계를 구분하고 토크·손실·전압·정상상태·실행 시간을 비교해야 합니다. 현재 새 모드의 실제 밀도·수렴·시간 개선은 미검증입니다. 새 물리 소스 지문과 기존 영점·데이터의 관계도 확인한 뒤 학습에 사용합니다.
