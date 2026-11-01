# Actual M2/M4 independent review

All three pairs pass the unchanged ten comparison limits at this single G1 / 1200 rpm / 100 A / beta 30° point. M4 same-instance saved-input semantics pass Root policy `5b3d970a…` with host `5241c305…` and typed rule `954173e1…`; independently replayed M2 also passes. These are diagnostic observations, not a certified profile.

| Case / task | Final Native triangles | Regional sum (duplicates) | Core W | Mean torque N m | Native transient min | CLI + autoextract min |
|---|---:|---:|---:|---:|---:|---:|
| M1 baseline / 100985 | 9,828 | 19,656 | 25.28471265 | 20.48104231 | 30.600 | 31.603 |
| M2 / 100989 | 17,502 | 35,004 | 25.56749412 | 20.48541613 | 55.333 | 56.372 |
| M4 / 100990 | 26,478 | 52,956 | 25.60847772 | 20.43230934 | 75.850 | 76.871 |

Final counts are the saved Native profile `Manual Refine` counts, independently matched to the sum over 38 distinct final CAD ParentPartIDs. The 91-row region sum is exactly twice the global count and must not be called global N. All cases record four cores for each of 1,801 matrix records, normal completion, nine complete 1,801-point raw grids, steady-state pass, and no physical/missing-output issues. Native stage durations are nested and are not summed.

| Target | M1 | M2 | M4 | M1 vs M4 error | M2 vs M4 error | Existing limit |
|---|---:|---:|---:|---:|---:|---:|
| Mean torque [N m] | 20.4810423 | 20.4854161 | 20.4323093 | 0.238509 % | 0.259916 % | 1 % |
| Peak torque [N m] | 25.1006717 | 25.0651816 | 25.0048062 | 0.383388 % | 0.241455 % | 2 % |
| Core loss [W] | 25.2847126 | 25.5674941 | 25.6084777 | 1.26429 % | 0.160039 % | 5 % |
| PM/solid loss [W] | 8.36363217 | 8.34359327 | 8.261593 | 1.2351 % | 0.992548 % | 5 % |
| Total loss [W] | 225.990081 | 226.252823 | 226.211807 | 0.0980169 % | 0.018132 % | 3 % |
| Estimated EM efficiency [%] | 91.9281005 | 91.9210601 | 91.9031114 | 0.0249891 pp | 0.0179487 pp | 0.5 pp |
| Ld [H] | 0.000675184769 | 0.000676264099 | 0.000680388316 | 0.764791 % | 0.606156 % | 2 % |
| Lq [H] | 0.000665246124 | 0.000667561903 | 0.000676287558 | 1.63265 % | 1.29023 % | 2 % |
| Physical phase peak [V] | 27.1878992 | 26.997101 | 27.0597496 | 0.47358 % | 0.231519 % | 2 % |
| Physical voltage span [V] | 48.2979253 | 48.4898335 | 48.2824755 | 0.0319989 % | 0.429469 % | 2 % |

The existing `quality._deltas` denominator is the stated reference value with its unchanged floor; M4 is the comparison reference here and is not assumed to be exact truth. No original threshold is violated. M1→M2 core difference is 1.1060%; M2→M4 is 0.16004%. Lq is the largest M1→M4 target discrepancy at 1.63265% against its existing 2% limit.

All three use the same parsed physical/time/control spec apart from mesh operation budgets. The per-group budgets (magnet, rotor, stator, winding, band) are M1 `[100,1000,1000,100,2000]`, M2 `[200,2000,2000,200,4000]`, M4 `[400,4000,4000,400,8000]`. They are input caps, not matched prescribed element lengths or equal mesh density. All runs used n113 and the same four-core wrapper path; concurrent load and coordinator retention differences remain timing caveats. M4 CLI wall is 1.364 times M2 and 2.432 times M1; these are observed costs, not a general speed model.

Power residuals are respectively +0.0105274%, −0.0403845%, +0.0123324%; the existing loaded physical gates pass. Torque power and efficiency retain the source electromagnetic scope, before friction/windage. No new net-shaft interpretation or numeric correction is applied.

Raw numerical replay uses the exact Source15 physical component. The cross-OS replay retains all original raw bytes; voltage and residual arithmetic differ from stored Linux JSON by at most 9.095e-13 in their stated units. Differences are listed explicitly, not called bit-exact. Initial self-imposed bit-exact comparison failure is retained in `review.log` and the attempt-1 script; it did not change any physical gate.

Every collection seal/index entry and ASCII/raw/gzip triple was independently checked, followed by a final rehash of all 522 bound inputs. M2 and M4 each have 170 sealed files / 56 triples. Baseline Source14 lacks a retained same-instance raw preimage: no retrospective semantic pass is claimed for it. M2/M4 preimages are cross-bound to their actual CLI intents before applying the unchanged Root policy.

All original task statuses remain failed/exit 1 with `solved_saved_closure_unproven`; their strict raw comparison remains false. New independent input-semantic evidence does not rewrite those facts. Training admission and profile convergence certification remain false. This study covers one loaded point and supports no all-geometry/all-speed promotion.

Input comparison SHA256: `f4ccc572de6e3a78a3cacd64ff93cde879a98cff81130513ebb7b3abdcf1ae41`. See `comparison.json`, `semantic_100990.json`, `semantic_100989.json`, the three numerical replay files, and `input_records.json` for exact bindings. Native/API/FEA/ledger/Y changes: zero.
