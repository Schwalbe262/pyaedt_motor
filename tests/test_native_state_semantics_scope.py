"""Complete model/definition input coverage and narrow native-result changes."""
import copy
import unittest
from tests.test_native_state_semantics import fixture, compare


class ScopeTests(unittest.TestCase):
    def test_known_additional_physical_inputs_remain_strict(self):
        for key, value in [("PerfectConductorThreshold", 1e20), ("InsulatorThreshold", 2),
                           ("BackgroundMaterialName", "air"), ("Optimetrics", {"new_setup": True}),
                           ("OutputVariable", {"EM_expression": "different"})]:
            with self.subTest(key=key):
                a, b, p = fixture()
                b["project"]["Maxwell2DModel"][key] = value
                self.assertFalse(compare(a, b, p)["semantic_equivalence_proven"])

    def test_unknown_or_missing_top_level_inputs_fail_even_consistently(self):
        for scope, key in [("Maxwell2DModel", "NewEMFeature"), ("Definitions", "NewCircuitDefinitions")]:
            a, b, p = fixture()
            for state in (a, b):
                state["project"][scope][key] = {"enabled": True}
            self.assertFalse(compare(a, b, p)["semantic_equivalence_proven"])
        a, b, p = fixture()
        for state in (a, b):
            del state["project"]["Maxwell2DModel"]["PerfectConductorThreshold"]
        self.assertFalse(compare(a, b, p)["semantic_equivalence_proven"])

    def test_extra_project_definitions_are_exact_including_generated_cosim(self):
        for key in ("Scripts", "Symbols", "DefInfo", "Compdefs"):
            a, b, p = fixture()
            b["project"]["Definitions"][key]["IPMSM"] = {"CosimDefinitions": {"DefaultCosim": "DefaultNetlist"}}
            self.assertFalse(compare(a, b, p)["semantic_equivalence_proven"], key)

    def test_known_result_display_and_counter_changes_can_pass(self):
        a, b, p = fixture()
        for state in (a, b):
            state["raw_variable_rows"].append("VariableProp('radius', 'D', '', '1mm')")
            state["project"]["Maxwell2DModel"]["SolutionManager"]["SimSetup"]["Solution"]["SimDataExtractor"]["Sweeps"]["Sweep"].append(
                {"Variable": "radius", "Column": "1mm", "Units": "mm"})
        model = b["project"]["Maxwell2DModel"]
        model["CacheHeaderFile"] = "HDRChanged123.tmp"
        extractor = model["SolutionManager"]["SimSetup"]["Solution"]["SimDataExtractor"]
        extractor["SimValue"][4] = "SimValueID=201"
        extractor["Sweeps"]["Sweep"][-1]["Column"] = "1.000000000001mm"
        extractor["Sweeps"]["Sweep"].reverse()
        result = compare(a, b, p)
        self.assertTrue(result["semantic_equivalence_proven"], result["errors"])
        self.assertEqual(result["change_categories"]["bounded_evaluated_length_display"], 1)

    def test_unknown_or_physical_change_in_result_channel_fails(self):
        for change in (lambda m: m["SolutionManager"].update(NewEMInput=True),
                       lambda m: m.update(CacheHeaderFile="../different_project"),
                       lambda m: m["SolutionManager"]["SimSetup"]["Solution"]["SimDataExtractor"]["SimValue"].__setitem__(0, "DifferentPhysics"),
                       lambda m: m["SolutionManager"]["SimSetup"]["Solution"]["SimDataExtractor"]["Sweeps"]["Sweep"][0].update(Column="1300rpm")):
            a, b, p = fixture()
            change(b["project"]["Maxwell2DModel"])
            self.assertFalse(compare(a, b, p)["semantic_equivalence_proven"])
        a, b, p = fixture()
        for state in (a, b):
            state["project"]["Maxwell2DModel"]["SolutionManager"]["UnknownResult"] = {}
        self.assertFalse(compare(a, b, p)["semantic_equivalence_proven"])

    def test_enabled_coupling_or_skew_is_unsupported_even_if_unchanged(self):
        for key in ("UseSkewModel", "EnableTranTranLinkWithSimplorer"):
            a, b, p = fixture()
            for state in (a, b):
                state["project"]["Maxwell2DModel"][key] = True
            self.assertFalse(compare(a, b, p)["semantic_equivalence_proven"])

    def test_project_datasets_and_instance_properties_are_strict(self):
        a, b, p = fixture()
        b["project"]["ProjectDatasets"] = {"new_BH_table": [0, 1]}
        self.assertFalse(compare(a, b, p)["semantic_equivalence_proven"])
        a, b, p = fixture()
        b["project"]["DataInstances"]["Instance"]["Maxwell2DDesignInstance"]["Properties"]["EMInput"] = 1
        self.assertFalse(compare(a, b, p)["semantic_equivalence_proven"])
        for scope in ("project", "instance"):
            a, b, p = fixture()
            for state in (a, b):
                target = state["project"] if scope == "project" else state["project"]["DataInstances"]["Instance"]["Maxwell2DDesignInstance"]
                target["unknown_input"] = True
            self.assertFalse(compare(a, b, p)["semantic_equivalence_proven"])

    def test_evaluated_display_nonfinite_huge_unit_or_duplicate_cannot_hide(self):
        for column in ("NaNmm", "infmm", "1e999999999mm", "1000mm", "1m"):
            a, b, p = fixture()
            for state in (a, b):
                state["raw_variable_rows"].append("VariableProp('radius', 'D', '', '1mm')")
                state["project"]["Maxwell2DModel"]["SolutionManager"]["SimSetup"]["Solution"]["SimDataExtractor"]["Sweeps"]["Sweep"].append(
                    {"Variable": "radius", "Column": "1mm", "Units": "mm"})
            b["project"]["Maxwell2DModel"]["SolutionManager"]["SimSetup"]["Solution"]["SimDataExtractor"]["Sweeps"]["Sweep"][-1]["Column"] = column
            self.assertFalse(compare(a, b, p)["semantic_equivalence_proven"], column)
        a, b, p = fixture()
        rows = b["project"]["Maxwell2DModel"]["SolutionManager"]["SimSetup"]["Solution"]["SimDataExtractor"]["Sweeps"]["Sweep"]
        rows.append(copy.deepcopy(rows[0]))
        self.assertFalse(compare(a, b, p)["semantic_equivalence_proven"])


if __name__ == "__main__":
    unittest.main()
