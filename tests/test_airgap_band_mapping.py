"""Opt-in v252 TAU2D O-gap treatment; inert SDK/OO data, never Native."""
import copy
import ast
from dataclasses import replace
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest import mock
import module.ipmsm_ppt_setup as ppt
from tests import test_ipmsm_ppt_setup_contract as contract_fixture

NAMES={"magnets":["PM"],"rotor":["Rotor"],"stator":["Stator"],"windings":["Coil"],"band":["Band"]}
OBJECTS={"magnet":["PM"],"rotor":["Rotor"],"stator":["Stator"],"winding":["Coil"],"band":["Band"]}
def chosen(angle=.6):return ppt.IPMSMPPTSpec(band_circle_segments=0,airgap_band_mapping_angle_deg=angle)
def properties(spec):
    ops={"Mesh_"+group:{"Objects":OBJECTS[group],"Type":"LengthBased","Enabled":True,"RefineInside":True,
        "RestrictElem":True,"RestrictLength":False,"NumMaxElem":str(count),"MaxLength":"1mm","ApplyToInitialMesh":False}
        for group,count in spec.mesh_elements.items()}
    ops["Mesh_AirGap"]={"Objects":["Band"],"Type":"CylindricalGap","UseBandMappingAngle":True,"BandMappingAngle":str(spec.airgap_band_mapping_angle_deg)+"deg"}
    return ops
def assigned(spec,returned=True,updated=True,design_type="Maxwell 2D",existing=None,deletion=True,events=None,native_deleted=True,cache_missing=False):
    length=[];gap=[]
    events=events if events is not None else[]
    native=copy.deepcopy(existing or{})
    handles=[]
    class Node:
        def __init__(self,p):self.p=p
        def GetPropNames(self):return list(self.p)
        def GetPropValue(self,k):return self.p[k]
    class Operation:
        def __init__(self,name):self.name=name;self.props={'Objects':['cached foreign object']}
        def delete(self):
            events.append('delete:'+self.name)
            if deletion is True:
                if native_deleted:del native[self.name]
                handles.remove(self)
            return deletion
    handles.extend(Operation(name)for name in native)
    if cache_missing:handles.clear()
    def make(**kw):
        length.append(kw);return NS(props={},update=lambda:True)
    def cylindrical(*args,**kw):
        if any(p['Type']=='CylindricalGap'for p in native.values()):raise AssertionError('duplicate CGT would reach SDK')
        gap.append((args,kw));return NS(props={"cached":"not evidence"},update=lambda:updated)if returned else returned
    mesh_node=NS(GetChildNames=lambda:list(native),GetChildObject=lambda name:Node(native[name]))
    app=NS(design_type=design_type,odesign=NS(GetChildObject=lambda name:mesh_node),
           mesh=NS(assign_length_mesh=make,assign_cylindrical_gap=cylindrical,meshoperations=handles))
    with mock.patch.object(ppt,"_get_group",side_effect=lambda m,g,k,f:g[k]):
        result=ppt.assign_mesh(app,NAMES,spec)
    return result,length,gap
class BandMappingAngleTests(unittest.TestCase):
    def test_default_retains_exact_five_length_ops_no_airgap(self):
        spec=ppt.IPMSMPPTSpec();result,length,gap=assigned(spec)
        self.assertEqual(list(result),["magnet","rotor","stator","winding","band"])
        self.assertEqual(gap,[]);self.assertEqual(len(length),5)
        self.assertEqual([x["maximum_elements"]for x in length],list(spec.mesh_elements.values()))
        self.assertTrue(all(x["maximum_length"]=="1mm"and x["inside_selection"]is True for x in length))
    def test_2d_optin_uses_supported_gap_api_not_3d_layers_or_band_length(self):
        result,length,gap=assigned(chosen())
        self.assertEqual(gap,[(('Band',),{"name":"Mesh_AirGap","band_mapping_angle":.6})])
        self.assertEqual(len(length),5);self.assertIn("airgap",result)
        self.assertTrue(all(x["maximum_elements"]is not None for x in length))
    def test_invalid_angle_and_segmented_band_reject_before_assignment(self):
        for angle in [True,False,".6",0,.00049,3.0001,float('nan'),float('inf')]:
            with self.subTest(angle=angle),self.assertRaises(ValueError):assigned(chosen(angle))
        with self.assertRaisesRegex(ValueError,"curved outline"):assigned(replace(chosen(),band_circle_segments=144))
        for angle in [.0005,3.]:self.assertTrue(assigned(chosen(angle))[2])
        with self.assertRaisesRegex(ValueError,"only Maxwell 2D"):assigned(chosen(),design_type="Maxwell 3D")
    def test_creation_failure_and_nonexact_update_cannot_be_accepted(self):
        for value in [False,None]:
            with self.subTest(returned=value),self.assertRaisesRegex(RuntimeError,"not created"):assigned(chosen(),returned=value)
        for value in [False,None,1]:
            with self.subTest(updated=value),self.assertRaisesRegex(RuntimeError,"exact True"):assigned(chosen(),updated=value)
    def test_saved_properties_bind_type_band_angle_and_no_3d_controls(self):
        spec=chosen();ops=properties(spec)
        audit=ppt.audit_mesh_operation_settings(ops,OBJECTS,spec,source="saved_AEDT")
        self.assertTrue(audit['passed']);self.assertFalse(audit['groups']['airgap']['actual_mesh_geometry_verified'])
        for key,value in [("Objects",['Region']),("Type","LengthBased"),("UseBandMappingAngle",1),
                          ("BandMappingAngle",".7deg"),("BandMappingAngle",".6rad"),("MovingSideLayers",2)]:
            bad=copy.deepcopy(ops);bad['Mesh_AirGap'][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):ppt.audit_mesh_operation_settings(bad,OBJECTS,spec,source="saved_AEDT")
    def test_native_readback_reads_actual_oo_not_cached_operation_props(self):
        spec=chosen();saved=properties(spec);actual={}
        for group in OBJECTS:
            p=saved['Mesh_'+group];actual['Mesh_'+group]={"Type":"Length Based","Enabled":True,"Assignment":','.join(p['Objects']),
                "Region":"Inside Selection","Restrict Max Elems":True,"Restrict Length":False,"Apply To Initial Mesh":False,"Max Elems":p['NumMaxElem'],"Max Length":"1mm"}
        actual['Mesh_AirGap']={"Type":"Cylindrical Gap Based","Assignment":"Band","Use Band Mapping Angle":True,"Band Mapping Angle":"0.6deg"}
        class Node:
            def __init__(self,p):self.p=p
            def GetPropNames(self):return list(self.p)
            def GetPropValue(self,k):return self.p[k]
        mesh=NS(GetChildObject=lambda name:Node(actual[name]))
        app=NS(odesign=NS(GetChildObject=lambda name:mesh),mesh=NS(meshoperations=[NS(props=saved['Mesh_AirGap'])]))
        with mock.patch.object(ppt,"_get_group",side_effect=lambda m,g,k,f:g[k]):
            self.assertTrue(ppt.audit_native_mesh_settings(app,NAMES,spec)['passed'])
            actual['Mesh_AirGap']['Assignment']='Foreign'
            with self.assertRaises(ValueError):ppt.audit_native_mesh_settings(app,NAMES,spec)
            actual['Mesh_AirGap']['Assignment']='Band';actual['Mesh_AirGap']['UseBandMappingAngle']=True
            with self.assertRaisesRegex(ValueError,'ambiguous'):ppt.audit_native_mesh_settings(app,NAMES,spec)
    def test_validation_drift_stops_before_analyze_or_setup_only_return(self):
        fixture=contract_fixture.IPMSMPPTSetupContractTests()
        for analyze in [False,True]:
            app=mock.Mock();app.validate_simple.return_value=1
            with fixture.patched_configure_helpers(app),mock.patch.object(ppt,'audit_native_mesh_settings',side_effect=[{'passed':True},ValueError('airgap changed')])as audit:
                with self.assertRaisesRegex(ValueError,'airgap changed'):ppt.configure_ipmsm_from_ppt(app,spec=chosen(),analyze=analyze)
                self.assertEqual(audit.call_count,2);app.analyze.assert_not_called()
    def test_rerun_cleans_only_named_airgap_and_original_mesh_operations(self):
        queried=[];deleted=[]
        class MeshModule:
            def GetOperationNames(self,kind):
                queried.append(kind)
                return ['Mesh_AirGap','OtherAirGap']if kind=='Cylindrical Gap Based'else['Mesh_band']
            def DeleteOp(self,names):deleted.extend(names)
        class Design:
            setup_names=[]
            odesign=NS(GetModule=lambda name:MeshModule()if name=='MeshSetup'else NS())
        result=ppt.clear_previous_ppt_setup(Design(),chosen())
        self.assertEqual(queried,['Length Based','Skin Depth Based','Surface Approximation Based','Cylindrical Gap Based'])
        self.assertEqual(deleted,['Mesh_band','Mesh_AirGap'])
        self.assertEqual(result['mesh'],deleted);self.assertNotIn('OtherAirGap',deleted)
    def test_actual_auto_cgt_shape_is_replaced_after_independent_band_binding(self):
        events=[]
        auto={'CylindricalGap1':{'Type':'CylindricalGap','Assignment':'Band','UseBandMappingAngle':False,'BandMappingAngle':'1deg'}}
        result,length,gap=assigned(chosen(),existing=auto,events=events)
        self.assertEqual(events,['delete:CylindricalGap1']);self.assertEqual(len(length),5)
        self.assertIn('airgap',result);self.assertEqual(gap[0][1]['name'],'Mesh_AirGap')
    def test_foreign_or_multiple_cgt_cannot_be_deleted(self):
        for native in [
            {'CylindricalGap1':{'Type':'CylindricalGap','Assignment':'Region'}},
            {'A':{'Type':'CylindricalGap','Assignment':'Band'},'B':{'Type':'CylindricalGap','Assignment':'Band'}},
        ]:
            events=[]
            with self.subTest(native=native),self.assertRaises(ValueError):assigned(chosen(),existing=native,events=events)
            self.assertEqual(events,[])
    def test_nonexact_public_delete_never_reaches_new_assignment(self):
        native={'CylindricalGap1':{'Type':'CylindricalGap','Assignment':'Band'}}
        for deletion in [False,None,1]:
            events=[]
            with self.subTest(deletion=deletion),self.assertRaisesRegex(RuntimeError,'delete.*exact True'):
                assigned(chosen(),existing=native,deletion=deletion,events=events)
            self.assertEqual(events,['delete:CylindricalGap1'])
    def test_public_delete_true_without_actual_native_removal_is_rejected(self):
        native={'CylindricalGap1':{'Type':'CylindricalGap','Assignment':'Band'}}
        with self.assertRaisesRegex(RuntimeError,'still exists'):
            assigned(chosen(),existing=native,native_deleted=False)
    def test_missing_public_sdk_cached_handle_rejects_without_refresh_or_delete(self):
        native={'CylindricalGap1':{'Type':'CylindricalGap','Assignment':'Band'}};events=[]
        with self.assertRaisesRegex(ValueError,'no exact public SDK operation'):
            assigned(chosen(),existing=native,cache_missing=True,events=events)
        self.assertEqual(events,[])
    def test_new_sdk_calls_exist_in_installed022_source_without_native_import(self):
        source=Path(sys.executable).parent/'Lib/site-packages/ansys/aedt/core/modules/mesh.py'
        classes={n.name:n for n in ast.parse(source.read_text(encoding='utf-8')).body if isinstance(n,ast.ClassDef)}
        mesh={n.name for n in classes['Mesh'].body if isinstance(n,ast.FunctionDef)}
        operations={n.name for n in classes['MeshOperation'].body if isinstance(n,ast.FunctionDef)}
        self.assertTrue({'meshoperations','assign_cylindrical_gap'}<=mesh)
        self.assertTrue({'delete','update'}<=operations)
        self.assertNotIn('refresh_mesh_operations',mesh)
        own=next(n for n in ast.parse(Path(ppt.__file__).read_text(encoding='utf-8')).body if isinstance(n,ast.FunctionDef)and n.name=='assign_mesh')
        self.assertNotIn('refresh_mesh_operations',{n.func.attr for n in ast.walk(own)if isinstance(n,ast.Call)and isinstance(n.func,ast.Attribute)})
if __name__=='__main__':unittest.main()
