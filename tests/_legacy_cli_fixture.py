"""Hash-preserved Source14/15 FS diagnostic fixtures, never production authority."""
from pathlib import Path
import gzip,hashlib,json
from pyaedt_motor import execution_compatibility as ec
D=Path(__file__).with_name("fixtures")/"compatibility_legacy"
MANIFEST=json.loads((D/"PROVENANCE.json").read_bytes())["files"]
def raw(name):
    item=MANIFEST[name];z=(D/(name+".gz")).read_bytes()
    if ec._record(z)!=item["gzip"]:raise AssertionError("legacy gzip fixture changed")
    value=gzip.decompress(z)
    if ec._record(value)!=item["raw"]:raise AssertionError("legacy raw fixture changed")
    return value

def document(name):return json.loads(raw(name))
def reference(name):return {"path":MANIFEST[name]["original_path"],**MANIFEST[name]["raw"]}
def sources(tag):return {n.removeprefix(tag+"/"):raw(n)for n in MANIFEST if n.startswith(tag+"/")}
def materialize(root,extras=()):
    root=Path(root);root.mkdir(parents=True,exist_ok=False)
    entries={MANIFEST[n]["original_path"]:raw(n)for n in MANIFEST if n=="binding"or n.startswith(("authority/","publication/"))}
    for ref,value in extras:
        if ec._record(value)!={k:ref[k]for k in("bytes","sha256")}:raise AssertionError("extra fixture seal differs")
        if ref["path"]in entries and entries[ref["path"]]!=value:raise AssertionError("first fixture authority differs")
        entries[ref["path"]]=value
    records={}
    for i,(original,value)in enumerate(entries.items()):
        path=root/("r"+str(i));path.write_bytes(value)
        records[str(i)]={"path":path.name,"original_path":original,**ec._record(value)}
    index=root/"index.json";index.write_bytes(ec.pilot.canonical_json({"schema":ec.db2.INDEX_SCHEMA,"files":records}))
    return ec.PortableEvidence(index,ec.pilot.record(index)["sha256"],root)
