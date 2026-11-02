"""Pure SDK profile issuance from actual raw qualification evidence; default dry.

No scheduler, reservation or Native action. A new output retains failures. The
normal selected JSON and the SDK proof document have separate references.
"""
import argparse
from pathlib import Path
import sys

if not __package__:
    sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2])]
    __package__ = 'pyaedt_motor'

from . import owned_sdk_production as sdk, sdk_launch as launch, quality, winding_frames as frames, production_zero


def _ref(path): return {'path':str(Path(path).resolve(strict=True)),**launch.record(path)}


def certify_profile(*, case_evidence, origin, frame, case_ids, reference_profile,
                    refined_reference_profile, selected_profile, output):
    for ref in (case_evidence,origin,frame): launch.read_ref(ref)
    destination=Path(output)
    sdk.need(not destination.exists(),'exclusive fresh SDK profile output required')
    sdk.need(case_ids and len(set(case_ids))==len(case_ids),'explicit distinct profile cases required')
    destination.mkdir(parents=True)
    evidence=sdk.OwnedSDKProductionEvidence.from_declaration(case_evidence['path'],case_evidence['sha256'])
    context=production_zero.VerifiedProductionZero.from_plan(origin['path'],origin['sha256'])
    manifest=sdk.document(launch.read_ref(frame))
    frame_evidence=frames.ProductionWindingEvidence(context,sdk_evidence=evidence)
    frames.validate_reference(manifest,frame_evidence)
    results,plans,mesh=[],[],{}
    for case_id in case_ids:
        proof=evidence.verify_case(case_id,expected_producer=manifest['producer_fingerprint'])
        sdk.need(proof['profile_evidence_eligible']is True,'profile issuance requires actual loaded smoke raw replay')
        results.append(proof['original_row']);plans.append(proof['case']);mesh[case_id]=proof['actual_mesh_elements']
    report=quality.compare_profiles(results,plans,reference_profile=reference_profile,
            refined_reference_profile=refined_reference_profile,mesh_counts=mesh,
            frame_manifest=manifest,frame_evidence=frame_evidence)
    report_path=destination/'comparison.json'
    with report_path.open('xb')as stream:stream.write(sdk.jb(report))
    selected=quality.selected_profile_manifest(report,report_path,selected_profile)
    proof={'schema':'owned_sdk_selected_profile_v1','backend':sdk.BACKEND,
           'producer_fingerprint':manifest['producer_fingerprint'],'case_evidence':case_evidence,
           'frame_evidence':{'path':origin['path'],'sha256':origin['sha256']},'reference_frame':manifest,
           'cases':list(case_ids),'reference_profile':reference_profile,'refined_reference_profile':refined_reference_profile,
           'selected_profile':selected_profile,'comparison_report':_ref(report_path),'settings':selected['settings']}
    proof_path=destination/'sdk_profile_proof.json'
    with proof_path.open('xb')as stream:stream.write(sdk.jb(proof))
    sdk.verify_selected_profile(proof,evidence,manifest['producer_fingerprint'],frame_evidence=frame_evidence)
    selected['owned_sdk_profile_proof']=_ref(proof_path)
    selected_path=destination/'selected_profile.json'
    with selected_path.open('xb')as stream:stream.write(sdk.jb(selected))
    sdk.verify_selected_profile_document(selected_path,expected_producer=manifest['producer_fingerprint'],
                                         expected_sha256=launch.record(selected_path)['sha256'])
    evidence.assert_unchanged();context.assert_unchanged()
    for ref in (case_evidence,origin,frame):launch.read_ref(ref)
    return {'comparison':_ref(report_path),'sdk_profile_proof':_ref(proof_path),'selected_profile':_ref(selected_path),
            'actual_new_source_FEAs_performed':0,'actual_training_accuracy_certified':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('case-evidence','origin','frame'):
        parser.add_argument('--'+name,type=Path,required=True);parser.add_argument('--'+name+'-sha256',required=True)
    parser.add_argument('--cases',nargs='+',required=True)
    for name in ('reference-profile','refined-reference-profile','selected-profile'):parser.add_argument('--'+name,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--certify',action='store_true')
    args=parser.parse_args(argv)
    if not args.certify:return 0
    refs={}
    for name in ('case_evidence','origin','frame'):
        ref=_ref(getattr(args,name));sdk.need(ref['sha256']==getattr(args,name+'_sha256'),'explicit profile input SHA differs')
        refs[name]=ref
    result=certify_profile(**refs,case_ids=args.cases,reference_profile=args.reference_profile,
           refined_reference_profile=args.refined_reference_profile,selected_profile=args.selected_profile,output=args.output)
    print(sdk.jb(result).decode())
    return 0


if __name__=='__main__':raise SystemExit(main())
