"""Typed research candidate only; no exception is installed in frozen st2."""
import copy
from . import native_state_semantics as strict

BASE_STRICT_SHA256 = "cb5d6ac0b294e2366844ca5a04160cbc93d1168574d55b321337093e9dac2a7b"
RULE_ID = "v252_empty_maxwell_current_defaultnetlist_descriptor_v1"
BASE = {"Library":"", "CircuitEnv":0, "Refbase":"U", "NumParts":1, "ModSinceLib":True,
        "Properties":{"TextProp":["Owner","SRD","","Maxwell 2D"]}, "CompExtID":6}
ADDED = {"Parameters":{"ButtonProp":["CosimDefinition","D","","Edit","Edit",40501,"ButtonPropClientData()"],
                       "MenuProp":["CoSimulator","D","","DefaultNetlist",0]},
         "CosimDefinitions":{"CosimDefinition":{"CosimulatorType":4,"CosimDefName":"DefaultNetlist",
             "IsDefinition":True,"Connect":True,"Data()":None,"GRef()":None},"DefaultCosim":"DefaultNetlist"}}
INSTANCE_PARAMS = {"MenuProp":["CoSimulator","OHD","","DefaultNetlist",0],
                   "ButtonProp":["CosimDefinition","OHD","","Edit","Edit",40501,"ButtonPropClientData()"]}
SOD_ADDED = {"IPMSM":{"CosimDefinition":{"CosimDefName":"DefaultNetlist","SODInstanceMap":{},"SODComponentList()":None}}}
EMPTY_EXTERNAL = {"ExternalCircuitFile[0:]":None,"ExternalCircuitFileExtenstion":"","InductorNames[0:]":None,
    "SourceNames[0:]":None,"SourceType[0:]":None,"ComponentInstanceName":"","ComponentInstanceID":-1,
    "OriginalPath":"","OriginalDesign":"","ParamValues":{},"ExternalSimulinkCircuitFileExtenstion":"",
    "MaxwellToSimulink[0:]":None,"SimulinkToMaxwell[0:]":None,"SimulinkFile":"","ExternalSimulinkCircuitFile":""}


def inspect_transition(before, after, *, include_projection=False):
    """Classify this exact addition; caller must separately issue/review policy.

    Does not prove same-instance identity, authorize normalization, or bypass any
    full-state, OS, numeric, source/frame/profile or training admission gate.
    """
    result={"rule_id":RULE_ID,"descriptor_pattern_matched":False,"candidate_rule_matched":False,"errors":[],"automatic_approval":False,
            "full_state_semantics_proven":False,"Root_policy_required":True,"new_same_instance_post_review_required":True}
    try:
        a,b=strict._extract(before),strict._extract(after)
        da,db=a["definitions"],b["definitions"]
        strict._need(strict._equal(da["Compdefs"],{"IPMSM":BASE}),"unreviewed before Compdefs")
        expected=copy.deepcopy(BASE);expected.update(copy.deepcopy(ADDED))
        strict._need(strict._equal(db["Compdefs"],{"IPMSM":expected}),"not exact empty default descriptor addition")
        result.update(descriptor_pattern_matched=True,added_subtrees=list(ADDED),added_leaves=19)
        data_a=before["project"]["DataInstances"];data_b=after["project"]["DataInstances"]
        strict._need("Parameters"not in data_a["CompInstances"]["Compinst"] and
                     strict._equal(data_b["CompInstances"]["Compinst"]["Parameters"],INSTANCE_PARAMS) and
                     strict._equal(data_a["SODInfo"],{}) and strict._equal(data_b["SODInfo"],SOD_ADDED),
                     "not exact empty instance/default registration addition")
        projected_instance=copy.deepcopy(b["instance_inputs"])
        del projected_instance[0]["CompInstances"]["Compinst"]["Parameters"]
        projected_instance[0]["SODInfo"]={}
        strict._need(strict._equal(a["instance_inputs"],projected_instance),"other design-instance input changed")
        for key in ("model_inputs","project_inputs","thermal","variables"):
            strict._need(strict._equal(a[key],b[key]),"strict residual input changed: "+key)
        strict._need(not any("TBOptiVariation" in row for row in a["variables"]),"Twin Builder variable unsupported")
        for name in strict.SCIENCE:
            if name!="ModelSetup":
                strict._need(strict._equal(a["science"][name],b["science"][name]),"strict science changed: "+name)
        for state in (before,after):
            model=state["project"]["Maxwell2DModel"]
            # _extract already rejects link/skew/components/stress/deformation.
            boundary=model["BoundarySetup"]
            strict._need(boundary["ProductSpecificData"]=={},"product-specific circuit data present")
            global_data=boundary["GlobalBoundData"]
            strict._need(all(k in global_data and strict._equal(global_data[k],v)for k,v in EMPTY_EXTERNAL.items()),
                         "external circuit/source/path/Simulink binding present")
            strict._need(strict._equal(global_data["OP_Option"],{"LinkType":"None"}) and
                         global_data["PWMExcitation"]["Windings[0:]"]is None,"operating-point/PWM link present")
            windings={k:v for k,v in boundary["Boundaries"].items()if isinstance(v,dict)and v.get("BoundType")=="Winding Group"}
            strict._need(set(windings)=={"PhaseA","PhaseB","PhaseC"},"unexpected winding group")
            ids=[]
            for name,w in windings.items():
                strict._need(w["Type"]=="Current"and w["IsSolid"]is False and w["IsComponent"]is False and
                             type(w["ID"])is int and w["ID"]>0 and isinstance(w["Current"],str)and w["Current"],
                             "noncanonical current winding: "+name)
                ids.append(w["ID"])
            strict._need(len(set(ids))==3,"duplicate winding IDs")
            setup=model["AnalysisSetup"]["SolveSetups"]["PPT_Transient"]
            strict._need(setup["UseControlProgram"]is False and setup["ControlProgramName"]==""and
                         setup["ControlProgramArg"]=="","control program present")
        projected=copy.deepcopy(db)
        for key in ADDED:
            del projected["Compdefs"]["IPMSM"][key]
        strict._need(strict._equal(da,projected),"other project definition changed")
        changes=[{"path":["Definitions","Compdefs","IPMSM",*path],"before":"__ABSENT__","after":value}
                 for path,value in strict._leaves(ADDED)]
        changes.extend({"path":["DataInstances","CompInstances","Compinst","Parameters",*path],"before":"__ABSENT__","after":value}
                       for path,value in strict._leaves(INSTANCE_PARAMS))
        changes.extend({"path":["DataInstances","SODInfo",*path],"before":"__ABSENT__","after":value}
                       for path,value in strict._leaves(SOD_ADDED))
        strict._need(len(changes)==33,"registration leaf coverage differs")
        changes.append({"path":["DataInstances","SODInfo","IPMSM","CosimDefinition","SODInstanceMap"],
                        "before":"__ABSENT__","after":{}})
        result.update(candidate_rule_matched=True,added_subtrees=list(ADDED),added_leaves=33,
                      added_empty_containers=1,
                      changed_paths=changes,
                      strict_Definitions_projection_equal=True,
                      scope="Only the exact empty generic descriptor addition; full raw originals remain authoritative.")
        if include_projection:
            result["projected_definitions"]=projected
            result["projected_instance_inputs"]=projected_instance
    except (ValueError,KeyError,TypeError,IndexError,AttributeError)as exc:
        result["errors"].append(str(exc))
    return result
