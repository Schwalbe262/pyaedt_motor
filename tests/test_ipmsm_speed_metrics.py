"""Expose speed-specific errors that a high aggregate R2 can conceal."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import numpy as np
import pandas as pd

from pyaedt_motor import train_physical as training
from pyaedt_motor.model_bundle import ALL_TARGETS


def frames(values=(1., 2., 3., 100., 200., 300.)):
    truth = pd.DataFrame({target: values for target in ALL_TARGETS})
    truth['input_base_rpm'] = [1200.] * 3 + [5000.] * 3
    truth['geometry_group_id'] = ['a', 'b', 'c', 'a', 'b', 'c']
    return truth, truth[list(ALL_TARGETS)].copy()


class SpeedMetricsTests(unittest.TestCase):
    def test_high_global_r2_does_not_conceal_low_speed_errors(self):
        truth, predicted = frames()
        predicted.loc[:2, list(ALL_TARGETS)] += 1.
        report = training.score_by_speed(truth, predicted)
        low, high = report['by_speed_rpm']
        for target in ALL_TARGETS:
            self.assertGreater(training.score(truth[target], predicted[target])['r2'], .95)
            self.assertLess(low['metrics'][target]['r2'], .95)
            self.assertEqual(high['metrics'][target]['r2'], 1.)
        self.assertFalse(report['used_for_selection'])
        self.assertTrue(report['diagnostic_only'])

    def test_reordered_predictions_are_rejected_instead_of_relabelled(self):
        truth, predicted = frames()
        with self.assertRaisesRegex(ValueError, 'uniquely aligned'):
            training.score_by_speed(truth, predicted.iloc[::-1])

    def test_duplicate_indices_are_rejected(self):
        truth, predicted = frames()
        truth.index = predicted.index = [0] * len(truth)
        with self.assertRaisesRegex(ValueError, 'uniquely aligned'):
            training.score_by_speed(truth, predicted)

    def test_invalid_speed_or_identity_cannot_drop_a_row(self):
        for column, value in [('input_base_rpm', np.nan), ('input_base_rpm', 0),
                              ('input_base_rpm', np.inf), ('geometry_group_id', None),
                              ('geometry_group_id', ' ')]:
            with self.subTest(column=column, value=value):
                truth, predicted = frames()
                truth.loc[0, column] = value
                with self.assertRaisesRegex(ValueError, 'positive speeds and geometry'):
                    training.score_by_speed(truth, predicted)

    def test_singleton_and_constant_targets_keep_mae_without_fabricating_r2(self):
        truth, predicted = frames((1., 1., 1., 3., 4., 5.))
        predicted.iloc[0, :] = 2.
        low = training.score_by_speed(truth, predicted)['by_speed_rpm'][0]
        self.assertEqual(low['metrics'][ALL_TARGETS[0]]['r2_undefined_reason'], 'constant_truth')
        self.assertIsNone(low['metrics'][ALL_TARGETS[0]]['r2'])
        self.assertAlmostEqual(low['metrics'][ALL_TARGETS[0]]['mae'], 1 / 3)
        one = training.score_by_speed(truth.iloc[[0]], predicted.iloc[[0]])['by_speed_rpm'][0]
        self.assertEqual(one['rows'], 1)
        self.assertEqual(one['geometry_groups'], 1)
        self.assertEqual(one['metrics'][ALL_TARGETS[0]]['r2_undefined_reason'], 'fewer_than_two_rows')
        self.assertEqual(one['metrics'][ALL_TARGETS[0]]['mae'], 1.)

    def test_nonfinite_prediction_is_retained_as_failed_diagnostic(self):
        truth, predicted = frames()
        predicted.loc[0, ALL_TARGETS[0]] = np.nan
        report = training.score_by_speed(truth, predicted)
        metric = report['by_speed_rpm'][0]['metrics'][ALL_TARGETS[0]]
        self.assertEqual(report['rows'], len(truth))
        self.assertFalse(metric['finite'])
        self.assertEqual(metric['r2_undefined_reason'], 'nonfinite_values')
        self.assertIsNone(metric['mae'])

    def test_partition_counts_each_row_once_and_counts_independent_geometries(self):
        truth, predicted = frames()
        truth.loc[1, 'geometry_group_id'] = 'a'
        order = [3, 0, 5, 2, 4, 1]
        report = training.score_by_speed(truth.iloc[order], predicted.iloc[order])
        self.assertEqual(sum(row['rows'] for row in report['by_speed_rpm']), len(truth))
        self.assertEqual([row['speed_rpm'] for row in report['by_speed_rpm']], [1200., 5000.])
        self.assertEqual([row['geometry_groups'] for row in report['by_speed_rpm']], [2, 3])
        self.assertTrue(all(set(row['metrics']) == set(ALL_TARGETS) for row in report['by_speed_rpm']))

    def test_training_publishes_diagnostics_after_family_selection(self):
        import train_ipmsm_lightgbm as legacy
        from tests.test_ipmsm_physical_bundle import spec, synthetic_rows
        events = []
        select = training.select_families
        by_speed = training.score_by_speed
        def selecting(*args, **kwargs):
            events.append('selection')
            return select(*args, **kwargs)
        def evaluating(*args, **kwargs):
            events.append('speed_evaluation')
            return by_speed(*args, **kwargs)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / 'input.csv'
            synthetic_rows().to_csv(data, index=False)
            prepared = legacy.prepare_training_data(legacy.require_training_dependencies(), [data], False, False, 1.5, v2=True)
            with mock.patch.object(training, 'select_families', side_effect=selecting), \
                 mock.patch.object(training, 'score_by_speed', side_effect=evaluating):
                bundle = training.fit_prepared(prepared, spec(), root / 'run', jobs=1, candidates=('physical_extra',))
            report = json.loads((root / 'run/report.json').read_text())
            self.assertEqual(events, ['selection', 'speed_evaluation'])
            self.assertEqual(report['test_metrics_by_speed'], bundle.metadata['test_metrics_by_speed'])
            self.assertEqual(report['test_metrics_by_speed']['rows'], report['rows']['test'])
            self.assertEqual(report['test_metrics'], bundle.metadata['test_metrics'])
            self.assertTrue(bundle.approved, bundle.metadata['promotion_issues'])


if __name__ == '__main__':
    unittest.main()
