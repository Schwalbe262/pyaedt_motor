from pathlib import Path
import hashlib,json
root=Path(__file__).resolve().parents[1]
manifest=json.loads((root/'file_manifest.json').read_text(encoding='utf-8'))
for name,item in manifest['files'].items():
    path=root/name
    if path.is_symlink() or not path.is_file():raise RuntimeError('missing/link: '+name)
    data=path.read_bytes()
    if len(data)!=item['bytes'] or hashlib.sha256(data).hexdigest()!=item['sha256']:raise RuntimeError('changed: '+name)
expected=set(manifest['files'])|{'file_manifest.json','package_seal.json'}
actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
if expected!=actual:raise RuntimeError('unexpected or missing archive files')
seal=json.loads((root/'package_seal.json').read_text(encoding='utf-8'))
for name,item in seal['files'].items():
    data=(root/name).read_bytes()
    if len(data)!=item['bytes'] or hashlib.sha256(data).hexdigest()!=item['sha256']:raise RuntimeError('seal changed: '+name)
native=root/'restore/c_native_325'
counts=[]
for name in ['simul_log_smoke/v4r4/base_v4r4.json','simul_log_smoke/v4r5_native/base_v4r5.json']:
    doc=json.loads((native/name).read_text(encoding='utf-8'));items=doc['pipeline']['immutable_inputs']
    for item in items:
        if hashlib.sha256((native/item['path']).read_bytes()).hexdigest()!=item['sha256']:raise RuntimeError('immutable mismatch')
    counts.append(len(items))
print(json.dumps({'status':'VERIFIED','files':len(actual),'immutable_counts':counts}))
