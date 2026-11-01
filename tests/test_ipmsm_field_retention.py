"""Optional transient field retention: pure spec/CSV/property tests, never Native."""
from dataclasses import asdict, replace
import csv
import io
import json
import math
import unittest
from unittest import mock

from module import ipmsm_ppt_setup as ppt
import run_ipmsm_batch as runner
from tests import test_ipmsm_output_error_control as native_fixtures


def schedule_case(**overrides):
    case = {"base_rpm": 1200, "pole_number": 8, "transient_periods": 3, "steps_per_period": 300,
            "save_fields_start_step": 600, "save_fields_stop_step": 900, "save_fields_every_n_steps": 25}
    case.update(overrides)
    return case


class FieldRetentionTests(unittest.TestCase):
    def test_default_none_preserves_actual32_and_prior_setup_fingerprint(self):
        spec = ppt.IPMSMPPTSpec()
        self.assertIsNone(ppt.field_save_schedule(spec))
        self.assertEqual(native_fixtures.native_setup(spec).props, native_fixtures.NATIVE32)
        # Captured from unchanged preimage run_ipmsm_batch.py's setup_fingerprint.
        self.assertEqual(runner.setup_fingerprint(spec, "default", "sin_current", False),
            "setup_v2:sha256:94bcab96601034db72b4863a86d4d914f89e021d8b32ad5819a2d35b5af43f07")
        for row in ({}, {"save_fields_every_n_steps": ""}, {"save_fields_every_n_steps": None}):
            self.assertIsNone(ppt.field_save_schedule(runner.build_spec(row)))

    def test_CSV_to_requested_spec_native_props_and_fingerprint(self):
        case = schedule_case()
        stream = io.StringIO(); writer = csv.DictWriter(stream, fieldnames=list(case)); writer.writeheader(); writer.writerow(case)
        stream.seek(0); spec = runner.build_spec(next(csv.DictReader(stream)))
        schedule = ppt.field_save_schedule(spec)
        self.assertEqual({k: schedule[k] for k in ("start_step", "stop_step", "every_n_steps", "expected_endpoint_snapshots")},
                         {"start_step": 600, "stop_step": 900, "every_n_steps": 25, "expected_endpoint_snapshots": 13})
        self.assertEqual((schedule["start_time_s"], schedule["stop_time_s"]), (.025, .0375))
        self.assertIs(schedule["Native_endpoint_inclusion_verified"], False)
        setup = native_fixtures.native_setup(spec)
        self.assertEqual({k: setup.props[k] for k in ("SaveFieldsType", "N Steps", "Steps From", "Steps To")},
            {"SaveFieldsType": "Every N Steps", "N Steps": "25", "Steps From": "0.025s", "Steps To": "0.0375s"})
        self.assertEqual(setup.updates, 1)
        self.assertEqual({k: v for k, v in setup.props.items() if k not in {"SaveFieldsType", "N Steps", "Steps From", "Steps To"}},
                         {k: v for k, v in native_fixtures.NATIVE32.items() if k != "SaveFieldsType"})
        requested = json.loads(json.dumps(asdict(spec)))
        for key in ("save_fields_start_step", "save_fields_stop_step", "save_fields_every_n_steps"):
            self.assertEqual(requested[key], case[key]); self.assertIn("input_" + key, runner.INPUT_SPEC_COLUMNS)
        disabled = replace(spec, save_fields_start_step=None, save_fields_stop_step=None, save_fields_every_n_steps=None)
        self.assertNotEqual(runner.setup_fingerprint(spec, "diag", "no_load", False),
                            runner.setup_fingerprint(disabled, "diag", "no_load", False))

    def test_prefixed_case_metadata_replays_without_dropping_schedule(self):
        original = runner.build_spec(schedule_case())
        row = {"input_" + key: str(value) for key, value in schedule_case().items() if key.startswith("save_fields_")}
        row.update(base_rpm=1200, pole_number=8, transient_periods=3, steps_per_period=300)
        self.assertEqual(ppt.field_save_schedule(runner.build_spec(row)), ppt.field_save_schedule(original))
        row["save_fields_every_n_steps"] = 50
        with self.assertRaisesRegex(ValueError, "conflicting integer aliases"): runner.build_spec(row)

    def test_enabled_schedule_replaces_stale_Custom_ranges(self):
        app = native_fixtures.FakeM2D()
        setup = native_fixtures.FakeSetup()
        setup.props.update(SaveFieldsType="Custom", SweepRanges={"Subrange": [{"RangeStart": "0s"}]})
        app.create_setup = mock.Mock(return_value=setup)
        with mock.patch.object(ppt, "_m2d", return_value=app):
            actual = ppt.create_ppt_transient_setup(object(), runner.build_spec(schedule_case()))
        self.assertNotIn("SweepRanges", actual.props)
        self.assertEqual(actual.props["SaveFieldsType"], "Every N Steps")
        self.assertEqual(actual.props["N Steps"], "25")
        app.analyze.assert_not_called()

    def test_enabled_schedule_sends_one_complete_update_with_auto_updating_SDK_props(self):
        app = native_fixtures.FakeM2D()
        setup = native_fixtures.FakeSetup()
        setup.auto_update = True
        snapshots = []
        original_update = setup.update

        def update():
            snapshots.append(dict(setup.props))
            return original_update()

        class AutoUpdatingProps(dict):
            # Model installed SDK SetupProps.__setitem__: each write can call update.
            def __setitem__(self, key, value):
                super().__setitem__(key, value)
                if setup.auto_update:
                    setup.update()

        setup.update = update
        setup.props = AutoUpdatingProps(setup.props)
        app.create_setup = mock.Mock(return_value=setup)
        with mock.patch.object(ppt, "_m2d", return_value=app):
            ppt.create_ppt_transient_setup(object(), runner.build_spec(schedule_case()))
        self.assertEqual(len(snapshots), 1, "partial Every-N settings must not be sent to Native")
        self.assertEqual({k: snapshots[0][k] for k in ("SaveFieldsType", "N Steps", "Steps From", "Steps To")},
                         {"SaveFieldsType": "Every N Steps", "N Steps": "25",
                          "Steps From": "0.025s", "Steps To": "0.0375s"})
        self.assertFalse(setup.auto_update)

    def test_unknown_retention_column_is_not_silently_disabled(self):
        for name in ("save_fields_every_n_step", "save_fields_type", "input_save_fields_count"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "unsupported field-retention"):
                runner.build_spec({name: "25"})

    def test_rejects_boolean_fractional_nonfinite_and_untyped_schedule_values(self):
        for value in (True, False, 25.0, 25.5, "25.0", "1e1", "nan", "inf", [], {}):
            for key in ("save_fields_start_step", "save_fields_stop_step", "save_fields_every_n_steps"):
                with self.subTest(key=key, value=repr(value)), self.assertRaisesRegex(ValueError, "integer"):
                    runner.build_spec(schedule_case(**{key: value}))

    def test_rejects_partial_out_of_grid_non_final_and_indivisible_schedules(self):
        invalid = [dict(save_fields_start_step=None), dict(save_fields_stop_step=None),
            dict(save_fields_every_n_steps=None), dict(save_fields_start_step=-1), dict(save_fields_stop_step=901),
            dict(save_fields_start_step=625), dict(save_fields_stop_step=899), dict(save_fields_every_n_steps=0),
            dict(save_fields_every_n_steps=-1), dict(save_fields_every_n_steps=301), dict(save_fields_every_n_steps=24)]
        for change in invalid:
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "save_fields"):
                runner.build_spec(schedule_case(**change))

    def test_grid_source_types_are_strict_only_when_retention_enabled(self):
        for key, value in (("transient_periods", True), ("steps_per_period", 300.5), ("pole_number", True), ("base_rpm", True)):
            with self.subTest(key=key), self.assertRaises(ValueError): runner.build_spec(schedule_case(**{key: value}))
        spec = runner.build_spec(schedule_case())
        for key, value in (("save_fields_start_step", False), ("transient_periods", 3.0), ("steps_per_period", 300.0),
                           ("pole_number", True), ("base_rpm", math.inf), ("base_rpm", 0), ("base_rpm", 1e308)):
            with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, "save_fields"):
                ppt.validate_ppt_spec_contract(replace(spec, **{key: value}))

    def test_time_bounds_follow_commanded_rpm_and_single_period_zero_start(self):
        spec = runner.build_spec(schedule_case(base_rpm=1234.56789))
        schedule = ppt.field_save_schedule(spec)
        self.assertEqual(schedule["stop_time_s"], 3 * 120 / (float(f"{spec.base_rpm:g}") * 8))
        one = runner.build_spec(schedule_case(transient_periods=1, save_fields_start_step=0, save_fields_stop_step=300))
        self.assertEqual(ppt.field_save_schedule(one)["start_time_s"], 0.)
        high = runner.build_spec(schedule_case(base_rpm=5000))
        self.assertEqual((ppt.field_save_schedule(high)["start_time_s"], ppt.field_save_schedule(high)["stop_time_s"]), (.006, .009))


if __name__ == "__main__": unittest.main()
