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
def assigned(spec,design_type="Maxwell 2D",existing=None,events=None,edit_return=None,
             apply_edit=True,edit_error=False,drift=None,cache_missing=False,observed=None):
    length=[];edits=[];events=events if events is not None else[]
    automatic={'CylindricalGap1':{'Type':'CylindricalGap','Assignment':'Band','ID':71,
                               'UseBandMappingAngle':False,'BandMappingAngle':'1deg'}}
    native=copy.deepcopy(automatic if existing is None else existing)
    initial=copy.deepcopy(native)
    class Node:
        def __init__(self,p):self.p=p
        def GetPropNames(self):return list(self.p)
        def GetPropValue(self,k):return self.p[k]
    def forbidden(*args,**kwargs):raise AssertionError('CGT delete/assign/cached update forbidden')
    def make(**kw):
        length.append(kw);return NS(props={},update=lambda:True)
    def edit(name,args):
        events.append('edit:'+name);edits.append((name,args))
        if edit_error:raise RuntimeError('Native edit failed')
        if apply_edit:native[name].update(UseBandMappingAngle=args[2],BandMappingAngle=args[4])
        if drift=='name':native['DifferentName']=native.pop(name)
        if drift=='type':native[name]['Type']='Length Based'
        if drift=='band':native[name]['Assignment']='Region'
        return edit_return
    mesh_node=NS(GetChildNames=lambda:list(native),GetChildObject=lambda name:Node(native[name]))
    handles=[]if cache_missing else[NS(name=name,type='Cylindrical Gap Based',props={'Objects':['cached foreign']},delete=forbidden,update=forbidden)for name in native]
    app=NS(design_type=design_type,odesign=NS(GetChildObject=lambda name:mesh_node,
           GetModule=lambda name:NS(EditCylindricalGapOp=edit)),
           mesh=NS(assign_length_mesh=make,assign_cylindrical_gap=forbidden,meshoperations=handles))
    with mock.patch.object(ppt,"_get_group",side_effect=lambda m,g,k,f:g[k]):
        result=ppt.assign_mesh(app,NAMES,spec)
    if observed is not None:observed.update(before=initial,after=copy.deepcopy(native))
    return result,length,edits
class BandMappingAngleTests(unittest.TestCase):
    def test_default_retains_exact_five_length_ops_no_airgap(self):
        spec=ppt.IPMSMPPTSpec();result,length,gap=assigned(spec)
        self.assertEqual(list(result),["magnet","rotor","stator","winding","band"])
        self.assertEqual(gap,[]);self.assertEqual(len(length),5)
        self.assertEqual([x["maximum_elements"]for x in length],list(spec.mesh_elements.values()))
        self.assertTrue(all(x["maximum_length"]=="1mm"and x["inside_selection"]is True for x in length))
    def test_2d_optin_edits_existing_operation_using_documented_native_void_API(self):
        # Requirement changed: preserve the rotation-created operation, never replace it.
        result,length,edits=assigned(chosen())
        self.assertEqual(edits,[('CylindricalGap1',['NAME:CylindricalGap1','UseBandMappingAngle:=',True,'BandMappingAngle:=','0.6deg'])])
        self.assertEqual(result['airgap']['operation_name'],'CylindricalGap1')
        self.assertEqual(len(length),5);self.assertTrue(all(x['maximum_elements']is not None for x in length))
    def test_invalid_angle_and_segmented_band_reject_before_assignment(self):
        for angle in [True,False,".6",0,.00049,3.0001,float('nan'),float('inf')]:
            with self.subTest(angle=angle),self.assertRaises(ValueError):assigned(chosen(angle))
        with self.assertRaisesRegex(ValueError,"curved outline"):assigned(replace(chosen(),band_circle_segments=144))
        for angle in [.0005,3.]:self.assertTrue(assigned(chosen(angle))[2])
        with self.assertRaisesRegex(ValueError,"only Maxwell 2D"):assigned(chosen(),design_type="Maxwell 3D")
    def test_missing_rotation_CGT_rejects_without_new_assignment(self):
        events=[]
        with self.assertRaisesRegex(ValueError,'exactly one existing'):
            assigned(chosen(),existing={},events=events)
        self.assertEqual(events,[])
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
        actual['AutomaticGap7']={"Type":"Cylindrical Gap Based","Assignment":"Band","Use Band Mapping Angle":True,"Band Mapping Angle":"0.6deg"}
        class Node:
            def __init__(self,p):self.p=p
            def GetPropNames(self):return list(self.p)
            def GetPropValue(self,k):return self.p[k]
        mesh=NS(GetChildNames=lambda:list(actual),GetChildObject=lambda name:Node(actual[name]))
        app=NS(odesign=NS(GetChildObject=lambda name:mesh),mesh=NS(meshoperations=[NS(props=saved['Mesh_AirGap'])]))
        with mock.patch.object(ppt,"_get_group",side_effect=lambda m,g,k,f:g[k]):
            actual_audit=ppt.audit_native_mesh_settings(app,NAMES,spec)
            self.assertTrue(actual_audit['passed']);self.assertEqual(actual_audit['groups']['airgap']['operation_name'],'AutomaticGap7')
            actual['AutomaticGap7']['Assignment']='Foreign'
            with self.assertRaises(ValueError):ppt.audit_native_mesh_settings(app,NAMES,spec)
            actual['AutomaticGap7']['Assignment']='Band';actual['AutomaticGap7']['UseBandMappingAngle']=True
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
    def test_actual_auto_CGT_name_ID_and_all_non_angle_properties_preserved(self):
        observed={};events=[]
        result,length,edits=assigned(chosen(),events=events,observed=observed)
        self.assertEqual(events,['edit:CylindricalGap1']);self.assertEqual(len(edits),1)
        before=observed['before'];after=observed['after']
        self.assertEqual(set(before),set(after));self.assertEqual(after['CylindricalGap1']['ID'],71)
        self.assertEqual({k:v for k,v in before['CylindricalGap1'].items()if k not in('UseBandMappingAngle','BandMappingAngle')},
                         {k:v for k,v in after['CylindricalGap1'].items()if k not in('UseBandMappingAngle','BandMappingAngle')})
        self.assertEqual(result['airgap']['UseBandMappingAngle'],True);self.assertEqual(len(length),5)
    def test_foreign_or_multiple_native_CGT_rejects_before_any_edit(self):
        for native in [
            {'CylindricalGap1':{'Type':'CylindricalGap','Assignment':'Region'}},
            {'A':{'Type':'CylindricalGap','Assignment':'Band'},'B':{'Type':'Cylindrical Gap Based','Assignment':'Band'}},
        ]:
            events=[]
            with self.subTest(native=native),self.assertRaises(ValueError):assigned(chosen(),existing=native,events=events)
            self.assertEqual(events,[])
    def test_native_edit_exception_or_nonvoid_return_is_not_accepted(self):
        with self.assertRaisesRegex(RuntimeError,'Native edit failed'):assigned(chosen(),edit_error=True)
        for value in [False,True,1]:
            with self.subTest(returned=value),self.assertRaisesRegex(RuntimeError,'non-None'):
                assigned(chosen(),edit_return=value)
    def test_void_return_alone_is_not_success_and_postedit_drift_rejects(self):
        with self.assertRaisesRegex(ValueError,'not applied'):assigned(chosen(),apply_edit=False)
        for drift in ('name','type','band'):
            with self.subTest(drift=drift),self.assertRaises(ValueError):assigned(chosen(),drift=drift)
    def test_native_alias_and_missing_or_stale_cached_SDK_handle_do_not_change_path(self):
        # SDK0.22's cached type alias is not its no-key update dispatch spelling.
        for native_type in ('CylindricalGap','Cylindrical Gap Based'):
            for missing in (False,True):
                native={'ActualAutoName':{'Type':native_type,'Assignment':'Band','ID':23,
                                        'UseBandMappingAngle':False,'BandMappingAngle':'1deg'}}
                result,_,edits=assigned(chosen(),existing=native,cache_missing=missing)
                self.assertEqual(result['airgap']['operation_name'],'ActualAutoName');self.assertEqual(len(edits),1)
    def test_installed022_update_dispatch_explains_supported_native_edit_choice(self):
        # Execute only the actual SDK method AST with inert objects; no SDK import/Native.
        source=Path(sys.executable).parent/'Lib/site-packages/ansys/aedt/core/modules/mesh.py'
        classes={n.name:n for n in ast.parse(source.read_text(encoding='utf-8')).body if isinstance(n,ast.ClassDef)}
        update=copy.deepcopy(next(n for n in classes['MeshOperation'].body if isinstance(n,ast.FunctionDef)and n.name=='update'))
        update.decorator_list=[];namespace={};exec(compile(ast.Module(body=[update],type_ignores=[]),str(source),'exec'),namespace)
        calls=[];payload=['NAME:AutoCGT','UseBandMappingAngle:=',True,'BandMappingAngle:=','0.6deg']
        module=NS(EditCylindricalGapOp=lambda name,args:calls.append((name,args)))
        mesh=NS(_app=NS(odesign=NS(GetChildObject=lambda name:NS(GetChildNames=lambda:['AutoCGT']))),omeshmodule=module)
        own=NS(_mesh=mesh,name='AutoCGT',type='CylindricalGap',_get_args=lambda:payload)
        self.assertIs(namespace['update'](own),True);self.assertEqual(calls,[('AutoCGT',payload)])
        calls.clear();own.type='Cylindrical Gap Based'
        self.assertIs(namespace['update'](own),False);self.assertEqual(calls,[])
        function=next(n for n in ast.parse(Path(ppt.__file__).read_text(encoding='utf-8')).body if isinstance(n,ast.FunctionDef)and n.name=='assign_mesh')
        invoked={n.func.attr for n in ast.walk(function)if isinstance(n,ast.Call)and isinstance(n.func,ast.Attribute)}
        self.assertIn('EditCylindricalGapOp',invoked);self.assertTrue({'delete','assign_cylindrical_gap','refresh_mesh_operations'}.isdisjoint(invoked))
    def test_saved_audit_discovers_actual_CGT_name_and_rejects_multiple(self):
        spec=chosen();ops=properties(spec);ops['ActualAutoName']=ops.pop('Mesh_AirGap')
        audit=ppt.audit_mesh_operation_settings(ops,OBJECTS,spec,source='saved_AEDT')
        self.assertEqual(audit['groups']['airgap']['operation_name'],'ActualAutoName')
        ops['SecondCGT']=dict(ops['ActualAutoName'])
        with self.assertRaisesRegex(ValueError,'exactly one'):ppt.audit_mesh_operation_settings(ops,OBJECTS,spec,source='saved_AEDT')
if __name__=='__main__':unittest.main()
