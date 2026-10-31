# Motor output definitions

`Moving1.Torque` is electromagnetic torque. The existing `output_mech_power_*_w`
values are that torque multiplied by prescribed angular speed, before modeled
friction or windage. They are not independently verified net shaft power.

`output_efficiency_*_pct` remains the existing loss-based estimate:
`100 * Tω / (Tω + CoreLoss + SolidLoss + CuLoss)`. It excludes mechanical and
inverter losses and must not be presented as certified net-shaft or drive-system
efficiency. The 7.5 kW design objective still needs an explicit rating definition;
this metadata change does not redefine it.

The following additive fields describe the unchanged values:

| Field | Meaning |
| --- | --- |
| `output_mech_power_scope` | Electromagnetic torque power; net shaft power unverified. |
| `output_efficiency_scope` | Estimated loss-based EM efficiency with omitted mechanical/inverter losses. |
| `output_power_balance_residual_normalization` | Signed residual percentage uses `max(abs(Pin), 1 W)`. |
| `output_power_balance_shared_copper_term` | The same lumped resistance/current term occurs in Pin and copper loss. |

Pin is reconstructed as the last-cycle mean of `Σ(dλ/dt + Ri)i`. Torque, core
loss and solid loss do not determine Pin algebraically. However, the shared
`RΣIrms²` term cancels in the power residual, so the residual cannot independently
validate resistance or the copper-loss model. Efficiency is also derived from
the torque/loss values, rather than being an independent measurement.

At zero winding current, Pin is zero. The existing signed residual in watts
remains meaningful; the percentage then uses the 1 W floor and is not efficiency.
No-load loss-normalized values, replacement thresholds or numerical corrections
are introduced. A nonzero watt discrepancy remains unresolved even when the
near-zero-input percentage is unsuitable for comparison.

Thermal `TemperatureSettings.EnableFeedback` is separate from magnetic
`BoundarySetup.GlobalBoundData.ConsiderCoreLossEffectOnField`. Enabling the latter
modifies the field using an estimated core loss; it does not by itself certify
the entire power partition. See the [Ansys v252 core-loss documentation](https://ansyshelp.ansys.com/public/Views/Secured/Electronics/v252/en/Subsystems/Maxwell/Content/Maxwell/SettingCoreLossforTransientandEddyCurrentSolvers.htm).

All existing numeric output names, values, model targets and admission gates are
unchanged. This scope metadata does not certify material, convergence or shaft
performance, and does not authorize subtracting core loss from torque again.
