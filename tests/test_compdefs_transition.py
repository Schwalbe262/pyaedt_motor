"""Portable synthetic registration tests; actual raw replay is separate evidence."""
import copy
from pathlib import Path
import unittest
from pyaedt_motor import compdefs_transition as rule
from pyaedt_motor import native_state_semantics as strict



class TransitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_registration_policy import synthetic_registration
        cls.before, cls.after, _ = synthetic_registration()

    def states(self):return copy.deepcopy(self.before),copy.deepcopy(self.after)

    def test_exact_33_registration_pattern_matches_without_approval(self):
        result=rule.inspect_transition(self.before,self.after)
        self.assertTrue(result["descriptor_pattern_matched"],result["errors"])
        self.assertTrue(result["candidate_rule_matched"],result["errors"])
        self.assertFalse(result["automatic_approval"])
        self.assertFalse(result["full_state_semantics_proven"])
        self.assertTrue(result["new_same_instance_post_review_required"])
        self.assertEqual(result["added_leaves"],33)

    def test_incomplete_19_only_transition_cannot_evade_33_path_contract(self):
        a,b=self.states()
        # An explicitly modified in-memory stand-in, not actual143 admission:
        # remove the separately observed 12 instance-parameter and two SODInfo
        # leaves only here. These are never exempted in the real classifier.
        del b["project"]["DataInstances"]["CompInstances"]["Compinst"]["Parameters"]
        b["project"]["DataInstances"]["SODInfo"]={}
        result=rule.inspect_transition(a,b)
        self.assertFalse(result["candidate_rule_matched"])
        self.assertFalse(result["automatic_approval"])

    def test_payload_type_value_and_unknown_fields_fail(self):
        for key,value in [("CosimulatorType",True),("CosimulatorType",5),("Connect",1),("Data()","netlist"),
                          ("GRef()",{}),("IsDefinition",1),("new_input",True)]:
            a,b=self.states();b["project"]["Definitions"]["Compdefs"]["IPMSM"]["CosimDefinitions"]["CosimDefinition"][key]=value
            self.assertFalse(rule.inspect_transition(a,b)["candidate_rule_matched"],key)
        a,b=self.states();b["project"]["Definitions"]["Compdefs"]["IPMSM"]["Parameters"]["ButtonProp"][5]=40501.0
        self.assertFalse(rule.inspect_transition(a,b)["candidate_rule_matched"])
        for change in (lambda p:p["CompInstances"]["Compinst"]["Parameters"]["MenuProp"].__setitem__(4,False),
                       lambda p:p["CompInstances"]["Compinst"]["Parameters"]["ButtonProp"].__setitem__(1,"D"),
                       lambda p:p["SODInfo"]["IPMSM"]["CosimDefinition"].update({"SODComponentList()":[]}),
                       lambda p:p["SODInfo"]["IPMSM"]["CosimDefinition"]["SODInstanceMap"].update(instance="active"),
                       lambda p:p["SODInfo"]["IPMSM"]["CosimDefinition"].update(Path="other.aedt")):
            a,b=self.states();change(b["project"]["DataInstances"])
            self.assertFalse(rule.inspect_transition(a,b)["candidate_rule_matched"])

    def test_enabled_external_paths_control_program_or_winding_rejected(self):
        changes=[lambda m:m.update(EnableTranTranLinkWithSimplorer=True),
            lambda m:m["BoundarySetup"]["Boundaries"]["PhaseA"].update(Type="External"),
            lambda m:m["BoundarySetup"]["GlobalBoundData"].update({"ExternalCircuitFile[0:]":"circuit.sph"}),
            lambda m:m["BoundarySetup"]["GlobalBoundData"]["OP_Option"].update(LinkType="Coupled"),
            lambda m:m["BoundarySetup"]["ProductSpecificData"].update(Circuit="external"),
            lambda m:m["AnalysisSetup"]["SolveSetups"]["PPT_Transient"].update(UseControlProgram=True)]
        for change in changes:
            a,b=self.states()
            for state in(a,b):change(state["project"]["Maxwell2DModel"])
            self.assertFalse(rule.inspect_transition(a,b)["candidate_rule_matched"])

    def test_strict_residual_definitions_and_new_inputs_rejected(self):
        a,b=self.states();b["project"]["Definitions"]["Scripts"]["script"]="execute"
        self.assertFalse(rule.inspect_transition(a,b)["candidate_rule_matched"])
        a,b=self.states();b["project"]["Definitions"]["Compdefs"]["IPMSM"]["NumParts"]=2
        self.assertFalse(rule.inspect_transition(a,b)["candidate_rule_matched"])
        a,b=self.states()
        for state in(a,b):state["project"]["Maxwell2DModel"]["new_EM_input"]=True
        self.assertFalse(rule.inspect_transition(a,b)["candidate_rule_matched"])

    def test_bool_duplicate_phase_ID_and_additional_group_rejected(self):
        for value in(True,25):
            a,b=self.states()
            for state in(a,b):state["project"]["Maxwell2DModel"]["BoundarySetup"]["Boundaries"]["PhaseB"]["ID"]=value
            self.assertFalse(rule.inspect_transition(a,b)["candidate_rule_matched"])
        a,b=self.states()
        for state in(a,b):
            boundaries=state["project"]["Maxwell2DModel"]["BoundarySetup"]["Boundaries"]
            boundaries["PhaseD"]=dict(boundaries["PhaseA"],ID=28)
        self.assertFalse(rule.inspect_transition(a,b)["candidate_rule_matched"])

    def test_classifier_does_not_mutate_input_trees(self):
        a,b=self.states();aa,bb=copy.deepcopy(a),copy.deepcopy(b)
        rule.inspect_transition(a,b)
        self.assertEqual(a,aa);self.assertEqual(b,bb)


if __name__=="__main__":unittest.main()
