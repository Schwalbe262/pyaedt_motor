"""Explicit production schema wiring; synthetic reservations, no Native."""
import copy
import unittest

from tests import _execution_contract_fixtures as fixtures
from pyaedt_motor import execution_contracts as contracts


class V2Contracts(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.Contracts(methodName="test_diagnostic_contract_and_acyclic_receipt")
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.fixture.contract["required_observed_cores"] = [4]
        self.fixture.contract["semantic_policy"] = None
        self.fixture.authorize(self.fixture.contract)

    def test_explicit_v2_receipt_and_future_actual_core_requirement(self):
        c = self.fixture.contract
        self.assertEqual(c["schema"], "public_owned_cli_execution_contract_v2")
        value = contracts.validate_contract(c)
        self.assertTrue(value["physics_compatibility_requirement"].startswith("physics-execution:sha256:"))
        self.assertFalse(value["future_Native_actual_cores_proven"])
        self.assertFalse(value["training_admission"])

    def test_core_requirement_bound_in_whole_contract_not_presence_marker(self):
        c = copy.deepcopy(self.fixture.contract)
        c["required_observed_cores"] = [1]
        self.assertNotEqual(contracts.prospective_physics_key(c), contracts.prospective_physics_key(self.fixture.contract))
        with self.assertRaisesRegex(ValueError, "whole contract"):
            contracts.validate_contract(c)
        c["required_observed_cores"] = [True]
        with self.assertRaisesRegex(ValueError, "required observed core"):
            contracts.validate_contract(c)

    def test_sdk_profile_cannot_masquerade_as_production_CLI_profile(self):
        # The old positive SDK fixture is still valid through its old API, but
        # explicit CLI selection must use actual FS/semantic/family/full mesh proof.
        self.fixture.production()
        self.fixture.contract["required_observed_cores"] = [4]
        self.fixture.authorize(self.fixture.contract)
        with self.assertRaisesRegex(ValueError, "semantic policy|actual family frame"):
            contracts.validate_contract(self.fixture.contract)


if __name__ == "__main__": unittest.main()
