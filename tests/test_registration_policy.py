"""Only synthetic raw records/policies: never an actual approval issuance."""
import copy
import hashlib
from pathlib import Path
import unittest
from tests.test_native_state_semantics import fixture, compare
from pyaedt_motor import compdefs_transition as rule


def synthetic_registration():
    a,b,p=fixture()
    for state in(a,b):
        project=state["project"];model=project["Maxwell2DModel"]
        model["AnalysisSetup"]={"SolveSetups":{"PPT_Transient":{"SetupType":"Transient","UseControlProgram":False,
            "ControlProgramName":"","ControlProgramArg":""}}}
        global_data=copy.deepcopy(rule.EMPTY_EXTERNAL)
        global_data.update(OP_Option={"LinkType":"None"},PWMExcitation={"Windings[0:]":None})
        model["BoundarySetup"]={"GlobalBoundData":global_data,"ProductSpecificData":{},"Boundaries":{
            name:{"BoundType":"Winding Group","ID":i,"Type":"Current","IsSolid":False,"IsComponent":False,"Current":"I(t)"}
            for name,i in zip(("PhaseA","PhaseB","PhaseC"),(25,26,27))}}
        project["Definitions"]["Compdefs"]={"IPMSM":copy.deepcopy(rule.BASE)}
        project["DataInstances"]["CompInstances"]={"Compinst":{"ID":"0","CompName":"IPMSM"}}
    b["project"]["Definitions"]["Compdefs"]["IPMSM"].update(copy.deepcopy(rule.ADDED))
    b["project"]["DataInstances"]["CompInstances"]["Compinst"]["Parameters"]=copy.deepcopy(rule.INSTANCE_PARAMS)
    b["project"]["DataInstances"]["SODInfo"]=copy.deepcopy(rule.SOD_ADDED)
    return a,b,p


class PolicyTests(unittest.TestCase):
    def test_default_st2_path_stays_strict_but_explicit_pinned_inert_policy_can_match(self):
        a,b,p=synthetic_registration()
        self.assertFalse(compare(a,b,p)["semantic_equivalence_proven"])
        p["output_registration_rule"]={"rule_id":rule.RULE_ID,"implementation_sha256":hashlib.sha256(Path(rule.__file__).read_bytes()).hexdigest(),"approved":True}
        result=compare(a,b,p)
        self.assertTrue(result["semantic_equivalence_proven"],result["errors"])
        self.assertEqual(result["change_categories"]["typed_empty_default_output_registration"],34)  # 33 scalar leaves + exact empty map.

    def test_wrong_or_unapproved_rule_bindings_fail(self):
        for item in ({"rule_id":rule.RULE_ID,"implementation_sha256":"0"*64,"approved":True},
                     {"rule_id":rule.RULE_ID,"implementation_sha256":hashlib.sha256(Path(rule.__file__).read_bytes()).hexdigest(),"approved":1}):
            a,b,p=synthetic_registration();p["output_registration_rule"]=item
            self.assertFalse(compare(a,b,p)["semantic_equivalence_proven"])

    def test_pinned_rule_does_not_permit_residual_input_or_registration_mutation(self):
        for change in (lambda x:x["Definitions"]["Scripts"].update(script="execute"),
                       lambda x:x["Maxwell2DModel"]["BoundarySetup"]["GlobalBoundData"].update({"ExternalCircuitFile[0:]":"c.sph"}),
                       lambda x:x["Definitions"]["Compdefs"]["IPMSM"]["CosimDefinitions"]["CosimDefinition"].update(Data="payload")):
            a,b,p=synthetic_registration();change(b["project"])
            p["output_registration_rule"]={"rule_id":rule.RULE_ID,"implementation_sha256":hashlib.sha256(Path(rule.__file__).read_bytes()).hexdigest(),"approved":True}
            self.assertFalse(compare(a,b,p)["semantic_equivalence_proven"])


if __name__=="__main__":unittest.main()
