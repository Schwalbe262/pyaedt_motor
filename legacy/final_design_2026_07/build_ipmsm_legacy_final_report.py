"""Build the final Korean NSGA-II versus FEA validation report."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from urllib.request import urlopen

import numpy as np
import pandas as pd


def _task(url: str, task_id: int) -> dict[str, object]:
    with urlopen(f"{url.rstrip('/')}/api/tasks/{task_id}", timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _duration_seconds(task: dict[str, object]) -> float:
    start = datetime.fromisoformat(str(task["started_at"]))
    finish = datetime.fromisoformat(str(task["finished_at"]))
    return (finish - start).total_seconds()


def _format_duration(seconds: float) -> str:
    total = int(round(seconds))
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _actual_front(frame: pd.DataFrame) -> pd.DataFrame:
    ordered = frame.sort_values(
        ["active_envelope_volume_l", "fea_efficiency_physical_pct"],
        ascending=[True, False],
    )
    selected = []
    best = -np.inf
    for _, row in ordered.iterrows():
        efficiency = float(row["fea_efficiency_physical_pct"])
        if efficiency > best + 1.0e-12:
            selected.append(row)
            best = efficiency
    return pd.DataFrame(selected)


def _actual_knee(front: pd.DataFrame) -> pd.Series:
    volume = front["active_envelope_volume_l"].to_numpy(dtype=float)
    efficiency = front["fea_efficiency_physical_pct"].to_numpy(dtype=float)
    normalized_volume = (volume - volume.min()) / (volume.max() - volume.min())
    efficiency_loss = (efficiency.max() - efficiency) / (
        efficiency.max() - efficiency.min()
    )
    return front.iloc[int(np.argmin(np.hypot(normalized_volume, efficiency_loss)))]


def _metric_row(name: str, values: dict[str, float]) -> str:
    return (
        f"| {name} | {values['MAE']:.3f} | {values['RMSE']:.3f} | "
        f"{values['bias']:+.3f} | {values['max_abs']:.3f} |"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--scheduler-url", default="http://127.0.0.1:8002")
    args = parser.parse_args()
    root = args.root.resolve()
    combined = root / "combined_final"
    validation = root / "fea_validation_10"
    comparison_dir = validation / "comparison"

    exact = pd.read_csv(combined / "global_exact_pareto.csv")
    candidates = pd.read_csv(combined / "pareto_candidate_set_200.csv")
    predictions = pd.read_csv(validation / "model_predictions_selected_10.csv")
    comparison = pd.read_csv(comparison_dir / "predicted_vs_fea.csv")
    summary = json.loads(
        (comparison_dir / "comparison_summary.json").read_text(encoding="utf-8")
    )
    manifest = json.loads(
        (validation / "scheduler_submit_manifest.json").read_text(encoding="utf-8")
    )
    joined = predictions.merge(
        comparison,
        on=["case_id", "candidate_id", "selection_order", "selection_role", "design_hash"],
        validate="one_to_one",
    ).sort_values("selection_order")
    actual_front = _actual_front(joined)
    knee = _actual_knee(actual_front)
    actual_front.to_csv(
        comparison_dir / "fea_actual_nondominated_5.csv", index=False, encoding="utf-8"
    )

    task_ids = [int(item["task_id"]) for item in manifest["submissions"]]
    tasks = [_task(args.scheduler_url, task_id) for task_id in task_ids]
    durations = [_duration_seconds(task) for task in tasks]
    batch_start = min(datetime.fromisoformat(str(task["started_at"])) for task in tasks)
    batch_finish = max(datetime.fromisoformat(str(task["finished_at"])) for task in tasks)
    batch_wall = (batch_finish - batch_start).total_seconds()

    metrics = summary["primary_error_metrics"]
    symmetry = summary["legacy_symmetry4_torque_diagnostic"]
    ratio = symmetry["predicted_to_raw_fea_ratio"]
    scaled = symmetry["predicted_minus_scaled_fea_error_metrics"]
    voltage = summary["fea_voltage_margin"]
    design_target = summary["design_torque_target"]

    result_rows = []
    for _, row in joined.iterrows():
        result_rows.append(
            "| {order} | {role} | {volume:.4f} | {pt:.2f} | {ft:.2f} | "
            "{pe:.2f} | {fe:.2f} | {de:+.2f} | {passed} |".format(
                order=int(row["selection_order"]),
                role=row["selection_role"],
                volume=float(row["active_envelope_volume_l"]),
                pt=float(row["predicted_torque_nm"]),
                ft=float(row["fea_torque_nm"]),
                pe=float(row["predicted_efficiency_physical_pct"]),
                fe=float(row["fea_efficiency_physical_pct"]),
                de=float(row["error_predicted_minus_fea_efficiency_physical_pct_point"]),
                passed="PASS" if bool(row["fea_design_target_pass"]) else "FAIL",
            )
        )

    front_ids = ", ".join(
        f"#{int(row.selection_order)}" for row in actual_front.itertuples()
    )
    min_volume = joined.loc[joined["active_envelope_volume_l"].idxmin()]
    max_efficiency = joined.loc[joined["fea_efficiency_physical_pct"].idxmax()]
    metric_rows = [
        _metric_row("Torque [N·m]", metrics["torque_nm"]),
        _metric_row("Physical efficiency [%p]", metrics["efficiency_physical_pct"]),
        _metric_row("Core loss [W]", metrics["core_loss_w"]),
        _metric_row("Solid loss [W]", metrics["solid_loss_w"]),
        _metric_row("Physical total loss [W]", metrics["total_loss_physical_w"]),
    ]

    report = f"""# Model + NSGA-II 모터 설계와 Reference-Ultra FEA 검증 보고서

작성일: 2026-07-20  
검증 판정: **REJECT_AND_RETRAIN — 현재 10개 설계 중 물리적으로 채택 가능한 설계 없음**

## 1. 결론

- 물리 보정 NSGA-II는 4개 seed, seed당 2,000 population × 1,000 generation으로 실행했다. 최종 population 8,000개는 모두 geometry·fill·보수적 surrogate torque 제약을 통과했다.
- 네 실행을 합친 strict global Pareto는 **{len(exact)}개**, 자료용 최종 candidate set은 그중 균등 추출한 **{len(candidates)}개 rank-1 설계**이다.
- Pareto에서 대표 10개를 골라 reference-ultra FEA를 독립 task 10개로 동시에 실행했으며 **10/10 성공, 실패 0**이다.
- 그러나 full-360, symmetry=1 실제 FEA torque는 **{joined['fea_torque_nm'].min():.2f}–{joined['fea_torque_nm'].max():.2f} N·m**로, 설계 목표 65.1 N·m를 만족한 설계는 **{design_target['fea_pass_count']}/10개**이다.
- legacy surrogate torque / actual FEA torque 비는 평균 **{ratio['mean']:.3f}배**이다. FEA torque에 과거 학습 계약의 symmetry multiplier 4를 진단 목적으로 적용하면 torque MAE가 **{scaled['MAE']:.3f} N·m**까지 감소한다. 즉 이번 실패의 지배 원인은 형상 최적화보다 **기존 학습 데이터의 ×4 torque 스케일 계약**이다.
- 따라서 이 Pareto를 최종 모터 설계로 승인하면 안 된다. 현재 산출물은 자료용 진단 결과이며, symmetry=1 데이터로 모델을 다시 학습한 뒤 NSGA-II를 재실행해야 한다.

![최종 NSGA-II Pareto](combined_final/final_nsga2_pareto_volume_efficiency.png)

## 2. 최종 NSGA-II 구성

사용 모델은 `lightgbm_batch2p199_batch3p174_disable_tuning_models`이며 raw 13,742행에서 품질·출력 필터 후 10,283행으로 학습됐다. 동일 test split에서 torque 평균 모델은 R² 0.838, MAE 4.691 N·m, core-loss 모델은 R² 0.727, solid-loss 모델은 R² 0.873이었다. 이 test 평가는 동일한 legacy ×4 계약 안에서 수행되어 이번 cross-contract 오류를 드러내지 못했다.

최종 재실행에서는 다음을 적용했다.

- 모든 modeled geometry 변수를 p174 학습범위와 설계 spec의 교집합으로 제한
- 모델에 없던 `slot_opening_ratio=0.09`, `magnet_space_height_ratio=1.0` 고정
- 학습 조건과 동일하게 stack length 49.45 mm 고정
- 직접 efficiency 모델 대신 torque/core/solid 예측과 geometry별 100 °C 동손으로 물리 효율 재계산
- 설계 목표 65.1 N·m에 torque test RMSE 6.251 N·m를 더한 **71.351 N·m**를 보수적 surrogate 제약으로 사용

최종 strict front 범위는 체적 **{exact['active_envelope_volume_l'].min():.4f}–{exact['active_envelope_volume_l'].max():.4f} L**, 예측 물리 효율 **{exact['predicted_efficiency_pct'].min():.3f}–{exact['predicted_efficiency_pct'].max():.3f}%**이다.

## 3. FEA 검증 조건과 실행

| 항목 | 값 |
|---|---:|
| 설계 수 / task 수 | 10 / 10 |
| 운전점 | 1200 rpm, 137.8 A peak, β=30° |
| beta convention | `legacy_phase_offset_v1` |
| stack / Vdc / 입력 Rphase | 49.45 mm / 200 V / 0.01 Ω |
| 모델 extent / symmetry | full-360 / 1 |
| transient | 12 periods, 150 steps/period |
| mesh elements | magnet 100, rotor 1000, stator 1000, winding 100, band 2000 |
| scheduler task IDs | {task_ids[0]}–{task_ids[-1]} |
| 병렬 batch wall time | {_format_duration(batch_wall)} |
| task runtime min / mean / max | {_format_duration(min(durations))} / {_format_duration(sum(durations)/len(durations))} / {_format_duration(max(durations))} |

학습 데이터는 90/120 step과 symmetry multiplier 4를 사용했지만 현재 검증 runner의 물리 계약은 full-360/symmetry=1이다. 이번 reference-ultra FEA는 모델과 동일 운전점의 고정밀 **cross-contract 검증**이며 byte-for-byte solver 재현은 아니다.

## 4. 설계별 Model/NSGA-II 예측과 FEA

주 효율은 양쪽 모두 geometry별 R100C와 137.8 A를 이용해 `core + solid + copper` 손실로 재계산했다. FEA native 효율은 별도 CSV에 보존했다.

| # | Selection role | Volume [L] | Pred T [N·m] | FEA T [N·m] | Pred η [%] | FEA η [%] | Pred−FEA [%p] | 65.1 N·m |
|---:|---|---:|---:|---:|---:|---:|---:|:---:|
{chr(10).join(result_rows)}

![모델 예측과 FEA Actual-Predicted](fea_validation_10/comparison/model_vs_fea_actual_predicted.png)

## 5. 오차 요약

오차 정의는 `Predicted − FEA`이다.

| 지표 | MAE | RMSE | Bias | Max abs |
|---|---:|---:|---:|---:|
{chr(10).join(metric_rows)}

- Torque: 모델이 평균적으로 약 55.49 N·m 과대예측했다.
- 물리 효율: 모델이 평균적으로 **{metrics['efficiency_physical_pct']['bias']:+.3f}%p** 과대예측했다. FEA 물리 효율 범위는 **{joined['fea_efficiency_physical_pct'].min():.3f}–{joined['fea_efficiency_physical_pct'].max():.3f}%**이다.
- 손실: 예측 total loss가 평균 **{metrics['total_loss_physical_w']['bias']:+.2f} W** 높았지만, torque가 약 4배 크게 예측되어 결과적으로 효율도 과대평가됐다.
- 1200 rpm raw voltage margin은 **{voltage['minimum_v']:.2f}–{voltage['maximum_v']:.2f} V**, 10/10 PASS였다. 단 FEA 입력 Rphase=0.01 Ω와 geometry R100C 차이 때문에 약 0.7–0.8 V 수준의 저항성 전압 편향 가능성이 있어 보조 지표로만 사용한다.

![FEA가 반영된 NSGA-II front](fea_validation_10/comparison/fea_validation_on_nsga2_front.png)

## 6. Legacy ×4 symmetry 진단

예측/실제 torque ratio는 최소 **{ratio['minimum']:.3f}**, 평균 **{ratio['mean']:.3f}**, 최대 **{ratio['maximum']:.3f}**이다. 진단용 `4 × FEA torque`와 예측 torque의 MAE는 **{scaled['MAE']:.3f} N·m**, RMSE는 **{scaled['RMSE']:.3f} N·m**이다. 이 일치는 과거 `change_symmetry_multiplier(4)`가 모델 target을 네 배 스케일로 만들었다는 강한 증거다.

**주의:** 4를 곱한 값은 원인을 확인하기 위한 진단값이며 승인 가능한 물리 FEA 결과가 아니다.

![legacy symmetry 진단](fea_validation_10/comparison/legacy_symmetry4_torque_diagnostic.png)

## 7. FEA 기준 Pareto와 참고 설계

10개 중 FEA 물리 효율–체적 기준 비지배 설계는 **{len(actual_front)}개**이며 번호는 {front_ids}이다.

- 최소체적 참고점 #{int(min_volume['selection_order'])}: {float(min_volume['active_envelope_volume_l']):.4f} L, FEA {float(min_volume['fea_torque_nm']):.2f} N·m, {float(min_volume['fea_efficiency_physical_pct']):.3f}%
- FEA knee 참고점 #{int(knee['selection_order'])}: {float(knee['active_envelope_volume_l']):.4f} L, FEA {float(knee['fea_torque_nm']):.2f} N·m, {float(knee['fea_efficiency_physical_pct']):.3f}%
- 최고 FEA 효율 참고점 #{int(max_efficiency['selection_order'])}: {float(max_efficiency['active_envelope_volume_l']):.4f} L, FEA {float(max_efficiency['fea_torque_nm']):.2f} N·m, {float(max_efficiency['fea_efficiency_physical_pct']):.3f}%

세 점 모두 65.1 N·m를 만족하지 않으므로 **참고용이며 채택 후보가 아니다**.

## 8. 다음 설계 루프

1. full-360/symmetry=1로 확정된 출력 계약에서 torque/core/solid 데이터와 모델을 재구축한다.
2. 과거 ×4 target을 학습·평가·NSGA 입력에서 제거하고, 동일 계약 holdout FEA로 torque scale을 먼저 검증한다.
3. 수정 모델로 torque 65.1 N·m, 1200 rpm voltage, 5000 rpm/7.5 kW 조건을 포함해 NSGA-II를 다시 실행한다.
4. 새 strict Pareto에서 최소 10개를 다시 reference-ultra FEA 검증하고, 실제 torque pass가 확인된 뒤에만 최종 설계를 승인한다.

## 9. 산출물

- 최종 Pareto 297개: [`combined_final/global_exact_pareto.csv`](combined_final/global_exact_pareto.csv)
- 자료용 rank-1 후보 200개: [`combined_final/pareto_candidate_set_200.csv`](combined_final/pareto_candidate_set_200.csv)
- FEA 대표 10개: [`combined_final/fea_representative_10.csv`](combined_final/fea_representative_10.csv)
- FEA case plan: [`fea_validation_10/fea_cases_10.csv`](fea_validation_10/fea_cases_10.csv)
- FEA 병합 결과: [`fea_validation_10/collected/merged_results.csv`](fea_validation_10/collected/merged_results.csv)
- 예측–FEA 상세 비교: [`fea_validation_10/comparison/predicted_vs_fea.csv`](fea_validation_10/comparison/predicted_vs_fea.csv)
- FEA actual Pareto 5개: [`fea_validation_10/comparison/fea_actual_nondominated_5.csv`](fea_validation_10/comparison/fea_actual_nondominated_5.csv)
- 비교 요약 JSON: [`fea_validation_10/comparison/comparison_summary.json`](fea_validation_10/comparison/comparison_summary.json)
"""

    report_path = root / "MODEL_NSGA2_VS_FEA_VALIDATION_REPORT.md"
    report_path.write_text(report, encoding="utf-8")
    report_summary = {
        "status": "complete",
        "decision": "REJECT_AND_RETRAIN",
        "report": str(report_path),
        "strict_pareto_designs": len(exact),
        "rank1_candidate_designs": len(candidates),
        "fea_completed": len(joined),
        "fea_design_target_pass": int(design_target["fea_pass_count"]),
        "fea_actual_nondominated": len(actual_front),
        "batch_wall_seconds": batch_wall,
        "task_ids": task_ids,
    }
    (root / "report_summary.json").write_text(
        json.dumps(report_summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report_summary, sort_keys=True))


if __name__ == "__main__":
    main()
