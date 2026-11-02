"""Explicit new winding protocol; all legacy reference APIs delegate unchanged."""
from dataclasses import asdict
from pathlib import Path
import hashlib,json
from . import reference_frames as legacy
from . import production_zero as zero

SCHEMA=legacy.SCHEMA
METHOD=legacy.METHOD
PREFIX=legacy.PREFIX
PRODUCTION_SCHEMA='ipmsm_production_winding_frame_v1'
PRODUCTION_METHOD='spec_bound_winding_reference_v1'
PRODUCTION_PREFIX='production-winding-frame:sha256:'
UNIT_PREFIX='inert-production-winding-frame:sha256:'
def __getattr__(name):return getattr(legacy,name)
def is_production(value):return value.get('schema_version')in (PRODUCTION_SCHEMA,zero.SCHEMA)
def is_family(value):return value.get('schema_version')==PRODUCTION_SCHEMA or legacy.is_family(value)
def is_frame_id(value):return str(value).startswith((PRODUCTION_PREFIX,UNIT_PREFIX,legacy.PREFIX))
def method_for(value):return PRODUCTION_METHOD if value.get('schema_version')==PRODUCTION_SCHEMA else legacy.METHOD
def implementation_sha256():return legacy.implementation_sha256()
def production_implementation_sha256():
    current=hashlib.sha256(Path(__file__).read_bytes()).hexdigest();zero.need(current==_IMPORTED_SHA,'production frame code changed after import');return current
def implementation_for(value):return production_implementation_sha256()if value.get('schema_version')==PRODUCTION_SCHEMA else legacy.implementation_sha256()

class ProductionWindingEvidence:
    def __init__(self,origin,*,sdk_evidence=None):
        zero.require_context(origin);self.origin=origin;self.sdk_evidence=sdk_evidence
        self._sdk_pin=zero.digest(sdk_evidence.cases)if sdk_evidence is not None else None
        self._origin_identity=id(origin);self._sdk_identity=id(sdk_evidence)
    def assert_unchanged(self):
        self.assert_memory_unchanged()
        self.origin.assert_unchanged()
        if self.sdk_evidence is not None:self.sdk_evidence.assert_unchanged()
    def assert_memory_unchanged(self):
        zero.need(id(self.origin)==self._origin_identity and id(self.sdk_evidence)==self._sdk_identity,'winding evidence object changed')
        self.origin.assert_memory_unchanged()
        zero.need((zero.digest(self.sdk_evidence.cases)if self.sdk_evidence is not None else None)==self._sdk_pin,'SDK declaration changed in memory')
        if self.sdk_evidence is not None:self.sdk_evidence.assert_memory_unchanged()

def _origin(evidence):
    origin=evidence.origin if type(evidence)is ProductionWindingEvidence else evidence
    zero.require_context(origin);return origin
def draft_reference(origin_manifest,evidence,*,assign_identity=False):
    if origin_manifest.get('schema_version')!=zero.SCHEMA:return legacy.draft_reference(origin_manifest,evidence,assign_identity=assign_identity)
    context=_origin(evidence);zero.validate_production_zero(origin_manifest,context.spec,context)
    scope=context.study.scope
    value={'schema_version':PRODUCTION_SCHEMA,'method':PRODUCTION_METHOD,'convention':origin_manifest['convention'],
        'protocol_implementation_sha256':production_implementation_sha256(),'production_zero_implementation_sha256':zero.implementation_sha256(),
        'origin_zero_manifest':origin_manifest,'electrical_zero_deg':origin_manifest['electrical_zero_deg'],
        'initial_position_deg':origin_manifest['initial_position_deg'],'producer_fingerprint':origin_manifest['producer_fingerprint'],
        'metric_contract':origin_manifest['metric_contract'],'back_emf_source':origin_manifest['back_emf_source'],
        'material_context':origin_manifest['material_context'],'winding_context':origin_manifest['winding_context'],
        'winding_spec':origin_manifest['winding_spec'],'material_fingerprint':origin_manifest['material_fingerprint'],
        'scope':scope,'origin_native_frames':[entry['native_frame']for entry in context.observations],
        'evidence_mode':origin_manifest['evidence_mode'],'training_admission':False}
    value['inferred_electrical_zero_deg']=origin_manifest['inferred_electrical_zero_deg']
    value['native_angle_binding']=dict(origin_manifest['native_angle_binding'])
    # A frame defines coordinates; it does not prove BEMF/mesh/torque accuracy across geometry.
    value['reference_frame_id']=(UNIT_PREFIX if context.inert else PRODUCTION_PREFIX)+zero.digest(value)if assign_identity else None
    return value
def validate_reference(manifest,evidence):
    if manifest.get('schema_version')!=PRODUCTION_SCHEMA:return legacy.validate_reference(manifest,evidence)
    expected=draft_reference(manifest['origin_zero_manifest'],evidence,assign_identity=True)
    zero.need(zero.canonical(manifest)==zero.canonical(expected),'production frame must replay its exact original evidence')
    return expected['electrical_zero_deg'],expected['reference_frame_id']
def resolve_reference(manifest,evidence=None):
    if manifest.get('schema_version')==PRODUCTION_SCHEMA:return validate_reference(manifest,evidence)
    if manifest.get('schema_version')==zero.SCHEMA:
        context=_origin(evidence);return zero.validate_production_zero(manifest,context.spec,context)
    return legacy.resolve_reference(manifest,evidence)
def semantic_context(manifest):
    if manifest.get('schema_version')!=PRODUCTION_SCHEMA:return legacy.semantic_context(manifest)
    return {'method':PRODUCTION_METHOD,'reference_frame_id':manifest['reference_frame_id'],'protocol_implementation_sha256':production_implementation_sha256(),
        'manifest_sha256':zero.digest(manifest),'scope_sha256':zero.digest(manifest['scope']),
        'electrical_zero_deg':manifest['electrical_zero_deg'],'initial_position_deg':manifest['initial_position_deg'],
        'origin_producer_fingerprint':manifest['producer_fingerprint'],'renderer_producer_fingerprint':manifest['producer_fingerprint']}
def frame_fields(manifest):
    if manifest.get('schema_version')!=PRODUCTION_SCHEMA:return legacy.frame_fields(manifest)
    return {'reference_frame_method':PRODUCTION_METHOD,'reference_frame_id':manifest['reference_frame_id']}
def require_spec_context(spec,manifest,evidence):
    context=_origin(evidence);zero.analyze_production_zero(spec,context)
    source=manifest['origin_zero_manifest']if manifest.get('schema_version')==PRODUCTION_SCHEMA else manifest
    zero.validate_production_zero(source,spec,context)
def apply_reference(spec,manifest,evidence=None):
    if not is_production(manifest):return legacy.apply_reference(spec,manifest,evidence)
    context=_origin(evidence);source=manifest['origin_zero_manifest']if is_family(manifest)else manifest
    updated=zero.apply_production_zero_to_spec(spec,source,context)
    angle,identity=resolve_reference(manifest,evidence)
    updated['beta_calibration']={'electrical_zero_deg':angle,'calibration_id':identity,'convention':manifest['convention']}
    if is_family(manifest):updated['reference_frame']=dict(manifest)
    return updated
def profile_context(profile,manifest):
    if manifest.get('schema_version')!=PRODUCTION_SCHEMA:return legacy.profile_context(profile,manifest)
    zero.need(profile.owned_sdk_profile_source is not None,'new winding production profile requires explicit SDK raw replay')
    source=profile.owned_sdk_profile_source
    from .owned_sdk_production import verify_selected_profile_document
    zero.need(zero.record(Path(source['path']).read_bytes())=={key:source[key]for key in ('bytes','sha256')},'SDK selected-profile source changed')
    verify_selected_profile_document(Path(source['path']),expected_producer=manifest['producer_fingerprint'],expected_sha256=source['sha256'])
    expected=semantic_context(manifest)
    for ref in profile.evidence:
        raw=Path(ref['path']).read_bytes();zero.need(hashlib.sha256(raw).hexdigest()==ref['sha256'],'selected-profile evidence changed')
        report=json.loads(raw)
        if report.get('reference_frame_context')==expected and report.get('winding_context')==manifest['winding_context']and report.get('material_fingerprint')==manifest['material_fingerprint']:return
    raise ValueError('certified profile lacks this actual winding frame/material context')

def validate_case_frame(case,spec,manifest,evidence):
    """Check coordinates and complete requested spec without admitting a row."""
    validate_reference(manifest,evidence)
    parsed=spec if hasattr(spec,'winding') else zero.load_spec(spec)
    require_spec_context(parsed,manifest,evidence)
    expected=zero.spec_context(parsed)
    expected.update(initial_position_deg=manifest['initial_position_deg'],
        electrical_zero_deg=manifest['electrical_zero_deg'],
        model_extent='full_360',symmetry_factor=1)
    for key,value in expected.items():legacy._equal(case.get(key),value,'actual winding frame '+key)
    return semantic_context(manifest)

def verify_profile_rows(rows,manifest,evidence):
    """Qualification supports numerical comparison, never production admission."""
    if manifest.get('schema_version')!=PRODUCTION_SCHEMA:return legacy.verify_rows(rows,manifest,evidence)
    validate_reference(manifest,evidence)
    zero.need(type(evidence)is ProductionWindingEvidence and evidence.sdk_evidence is not None,'typed SDK profile evidence required')
    from .owned_sdk_production import OwnedSDKProductionEvidence
    zero.need(type(evidence.sdk_evidence)is OwnedSDKProductionEvidence,'explicit SDK profile evidence required')
    for row in rows:
        proof=evidence.sdk_evidence.verify_case(row['case_id'],expected_producer=manifest['producer_fingerprint'])
        zero.need(proof['profile_evidence_eligible']is True,'only fully verified diagnostic node-smoke evidence may certify a numerical profile')
        zero.need(zero.canonical(proof['original_row'])==zero.canonical(row),'profile row differs from actual raw evidence')
        zero.need(row.get('input_beta_calibration_id')==manifest['reference_frame_id'],'profile row uses another frame identity')
        validate_case_frame(proof['full_spec42'],_origin(evidence).spec,manifest,evidence)
    return semantic_context(manifest)

def verify_rows(rows,manifest,evidence):
    if manifest.get('schema_version')!=PRODUCTION_SCHEMA:return legacy.verify_rows(rows,manifest,evidence)
    validate_reference(manifest,evidence)
    zero.need(type(evidence)is ProductionWindingEvidence and evidence.sdk_evidence is not None,'typed actual SDK production case evidence required')
    from .owned_sdk_production import OwnedSDKProductionEvidence
    zero.need(type(evidence.sdk_evidence)is OwnedSDKProductionEvidence,'explicit SDK production evidence required; no CLI/SDK fallback')
    proofs=evidence.sdk_evidence.verify_frame_rows(rows,manifest)
    zero.need(len(proofs)==len(rows)and len({row['case_id']for row in rows})==len(rows),'complete unique production frame cases required')
    admitted=[]
    for row,proof in zip(rows,proofs):
        zero.need(proof['production_eligible']is True,'qualification/diagnostic cannot enter production frame')
        full=proof['full_spec42']
        for key,value in {**manifest['material_context'],**manifest['winding_context'],'initial_position_deg':manifest['initial_position_deg'],'electrical_zero_deg':manifest['electrical_zero_deg'],'model_extent':'full_360','symmetry_factor':1}.items():legacy._equal(full.get(key),value,'production winding actual spec '+key)
        admitted.append({'case_id':row['case_id'],'full_spec':full,'source_files':proof['source_files'],'receipt':proof['receipt']})
    evidence.assert_unchanged();admitted.sort(key=lambda item:item['case_id'])
    return {**semantic_context(manifest),'admitted_cases':len(admitted),'admitted_case_ids':[item['case_id']for item in admitted],'case_audits_sha256':zero.digest(admitted)}

def verify_domain(domain,manifest,evidence):
    if manifest.get('schema_version')!=PRODUCTION_SCHEMA:return legacy.verify_domain(domain,manifest,evidence)
    zero.need(type(evidence)is ProductionWindingEvidence and evidence.sdk_evidence is not None,'typed production domain evidence required')
    expected=domain.get('reference_frame_context')
    zero.need(type(expected)is dict and expected.get('admitted_case_ids'),'model domain lacks actual winding-frame admissions')
    rows=[evidence.sdk_evidence.verify_case(case,expected_producer=manifest['producer_fingerprint'])['production_row']for case in expected['admitted_case_ids']]
    zero.need(zero.canonical(verify_rows(rows,manifest,evidence))==zero.canonical(expected),'model domain and actual winding case admissions differ')
def verify_spec_rows(spec,rows,evidence=None):
    manifest=spec.get('reference_frame')
    if manifest is None or manifest.get('schema_version')!=PRODUCTION_SCHEMA:
        zero.need(not any(is_frame_id(r.get('input_beta_calibration_id',''))and not str(r.get('input_beta_calibration_id','')).startswith(legacy.PREFIX)for r in rows),'new winding frame requires explicit source spec')
        return legacy.verify_spec_rows(spec,rows,evidence)
    angle,identity=validate_reference(manifest,evidence)
    zero.need(spec['beta_calibration']=={'electrical_zero_deg':angle,'calibration_id':identity,'convention':manifest['convention']},'spec calibration differs from its production frame')
    require_spec_context(zero.load_spec(spec),manifest,evidence);return verify_rows(rows,manifest,evidence)
def evidence_arguments(parser):
    legacy.evidence_arguments(parser);parser.add_argument('--production-zero-evidence',type=Path);parser.add_argument('--production-zero-evidence-sha256')
    parser.add_argument('--owned-sdk-production-evidence',type=Path);parser.add_argument('--owned-sdk-production-evidence-sha256')
def evidence_from_arguments(args):
    path=getattr(args,'production_zero_evidence',None);sha=getattr(args,'production_zero_evidence_sha256',None)
    zero.need(bool(path)==bool(sha),'production origin path and SHA must be supplied together')
    sdk_path=getattr(args,'owned_sdk_production_evidence',None);sdk_sha=getattr(args,'owned_sdk_production_evidence_sha256',None)
    zero.need(bool(sdk_path)==bool(sdk_sha),'SDK production evidence path/SHA required together')
    if path:
        sdk=None
        if sdk_path:
            from .owned_sdk_production import OwnedSDKProductionEvidence
            sdk=OwnedSDKProductionEvidence.from_declaration(sdk_path,sdk_sha)
        return ProductionWindingEvidence(zero.VerifiedProductionZero.from_plan(path,sha),sdk_evidence=sdk)
    zero.need(not sdk_path,'SDK winding evidence requires its explicit production-zero origin')
    return legacy.evidence_from_arguments(args)

_IMPORTED_SHA=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
