# Physical-surrogate design proposals

`pyaedt_motor.optimize_physical` consumes the new approved physical bundle. It
does not load a legacy v2/reference-ultra model, submit FEA, or read held-out
target values. Every exported design remains a proposal pending independent
FEA at all requested operating points.

The adapter requires four independent inputs: the bundle directory, its exact
original training specification, the complete expected fingerprint mapping,
and the hashed v3 physical-zero manifest. The fingerprint mapping must come
from the intended producer/material/setup/frame context; copying it from an
arbitrary artifact does not establish that context. The original specification
is hashed before the separate zero manifest is applied for parsing. Thus a
training specification without a `beta_calibration` block remains usable.
Changed winding, engineering bounds, topology, speed targets, calibration,
source specification or fingerprint context are refused. Rejected models have
no optimization override.

The operating requirements are 7.5 kW at 1200 and 5000 rpm, 200 V DC, at most
137.8 A peak, 6.36 A/mm² RMS conductor current density, 0.45 slot fill, 300 mm
outer diameter and 40–70 mm stack length. Resistance uses the shared 100 °C
winding/end-turn calculation. The inactive `stator_shoe_thick` stays at 1.5;
changing it cannot improve a candidate. Stack length is normalized to the six
significant digits currently applied by FEA before features, resistance and
volume are computed. Stator/rotor derived inputs use the same exact six-field
helper as the checked training/prediction contract.

For each geometry and speed the default control search predicts a full
33-current by 9-beta grid in one DataFrame call. It then refines up to three
promising controls in each of two bounded batches. Current and beta stay
inside the declared domain. Rows outside the observed training-coordinate box
are filtered before model prediction and counted explicitly. An unsupported
prediction or a finite search miss is not evidence that a motor is physically
infeasible. A support box also does not prove dense coverage of joint geometry
and control combinations.

A control is accepted only when its torque lower bound meets the required
torque, its voltage-span upper bound is at most `0.95 * Vdc = 190 V`, and the
current/current-density/fill limits hold. Voltage span means the maximum over
time of `max(Va,Vb,Vc) - min(Va,Vb,Vc)`. Under ideal averaged SVM with optimal
common-mode injection this checks whether all three phase voltages fit inside
the available DC bus. The phase-to-neutral peak remains a separate diagnostic;
its sinusoidal `Vdc/sqrt(3)` relation is not imposed on arbitrary waveforms.
Bundles missing the span output are refused. Real switching, dead time and
controller dynamics still need independent verification.

Feasible controls minimize the calibrated upper bound of total loss. The two
design objectives are active envelope volume and duty-weighted upper total
loss. Exports retain mean/lower/upper values for every output, each speed's
current/beta/Id/Iq, resistance, copper loss, constraint margins, support counts,
and operating targets. Aggregate cycle efficiency uses weighted mechanical
power and loss, with interval endpoints combined monotonically. The Pareto
front contains predicted feasible proposals only.

The default full-geometry mode evaluates the explicit baseline plus feasible
Latin-hypercube samples over all fourteen active geometry variables and stack
length, using the same deterministic pure DOE sampler as dataset planning.
Only positive geometry, declared bounds and slot fill exclude samples; the
prediction adapter then reports unsupported training-domain combinations.
The default plan has 32 designs. `--count` controls this sampling stage and
`--max-design-evaluations` caps the entire submitted prediction plan (256 by
default, 2048 maximum). Each design has at most six model batches for the two
operating speeds and their control refinements.

For an explicitly faster local search, `--mode local_subspace` changes only
outer radius and stack length around the baseline. Its `--variables` argument
can declare another active subspace; every other geometry value stays fixed.
`search_designs` also accepts a complete explicit plan. These finite sampling
and local control refinements do not establish a global optimum. No controller
or additional NSGA-II population is silently trained or launched. The model's
uncertainty is calibrated on independent geometry groups; it is not a new
validation of every adaptively selected design.

Example from the repository with the supported Python environment:

```powershell
$env:PYTHONPATH = "src;."
& C:/Users/peets/anaconda3/envs/pyaedt2026v1/python.exe -m pyaedt_motor.optimize_physical `
  --bundle C:/Users/peets/NEC/pyaedt_motor_runtime/rebuild_20261030/train/artifact `
  --spec C:/Users/peets/NEC/pyaedt_motor_runtime/rebuild_20261030/train/source_spec.json `
  --expected-fingerprints C:/Users/peets/NEC/pyaedt_motor_runtime/rebuild_20261030/expected_context.json `
  --calibration-manifest C:/Users/peets/NEC/pyaedt_motor_runtime/rebuild_20261030/physical_zero_manifest_v3.json `
  --baseline C:/Users/peets/NEC/pyaedt_motor_runtime/rebuild_20261030/baseline_geometry.json `
  --count 32 --seed 42 `
  --output C:/Users/peets/NEC/pyaedt_motor_runtime/rebuild_20261030/proposals.json
```

The example paths describe future approved artifacts; their existence and
approval are not assumed. Baseline JSON contains all fifteen independent
geometry values plus `stack_length_mm`. Reports record the full design plan,
seed, specification/calibration/model/source provenance, support domain and
self-content SHA256. Publication is exclusive, verifies written bytes, and
requires the C runtime drive on Windows. Reuse a new output filename for each
run. No completed report means no published proposal set.
