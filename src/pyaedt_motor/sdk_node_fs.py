"""Explicit diagnostic node FS/import qualification; zero Native calls/FEAs.

This is the only bootstrap step without preceding Native proof. It may run only
as a fresh isolated job with separate Root source/node/install authority.
"""
import importlib
import os
from pathlib import Path
import socket
import sys

if not __package__:
    sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2])]
    __package__ = 'pyaedt_motor'

from . import sdk_launch as launch, owned_sdk_production as sdk, _sdk_launch_resource as resource


def capture(profile_ref, source_root, output):
    profile = sdk.document(launch.read_ref(profile_ref))
    sdk.need(profile.get("schema") == "owned_sdk_node_fs_job_v1" and profile.get("execution_enabled") is True,
             "explicit enabled FS-only job required")
    sdk.need(os.name == "posix" and os.getuid() == os.geteuid() == 1641
             and socket.gethostname().split(".")[0] == profile["node"], "actual own Linux node/UID required")
    sdk.need(profile["purpose"] == "diagnostic" and profile["FEAs"] == 0 and type(profile["FEAs"]) is int,
             "FS bootstrap must remain diagnostic FEA0")
    approval = sdk.document(launch.read_ref(profile["root_approval"]))
    sdk.need(approval.get("schema") == "Root_owned_sdk_node_FS_authority_v1" and approval.get("execution_authorized") is True
             and approval.get("source_reviewed") is True and approval.get("installation_import_reviewed") is True,
             "separate Root FS/import authority required")
    sdk.need(approval.get('evidence_mode') == 'linux_original' and 'inert' not in str(approval.get('scope','')).casefold(),
             'actual FS entry rejects inert/test/unspecified authority')
    for key in ("source_files", "source_commit", "node"):
        sdk.exact(approval.get(key), profile[key], "Root FS authority " + key)
    sdk.need(set(launch.PHYSICAL + launch.ENTRIES + sdk.EXTRA_SOURCE) <= set(profile["source_files"]), "complete Source19 closure required")
    # Reuse only FS activation, never construct Context/LaunchAuthority or Native.
    context = launch.Context.__new__(launch.Context)
    context.profile = profile; context.root = Path(source_root).resolve(strict=True)
    context.ref = profile_ref
    context.production = None
    context.check_inputs(); context.activate()
    gate, _ = resource.capture_current(type("ReadOnly", (), {"record": staticmethod(launch.record)})(), os.environ,
                        policy={"node": profile["node"], "UID": 1641, "account": "dw16", "minimum_job_CPUs": profile["minimum_job_CPUs"]})
    core = importlib.import_module("ansys.aedt.core")
    imported = {}
    for name, module in list(sys.modules.items()):
        if name == "pyaedt" or name.startswith(("pyaedt.", "ansys.aedt.core")):
            path = getattr(module, "__file__", None)
            if path:
                path = Path(path).resolve(strict=True)
                sdk.need(path.is_relative_to(context.sdk) and launch.record(path) == context.manifest[path.relative_to(context.sdk).as_posix()],
                         "mixed SDK import during FS qualification")
                imported[name] = str(path)
    sdk.need(imported and Path(core.__file__).resolve().is_relative_to(context.sdk), "actual private SDK import required")
    context.check_inputs(); context.verify_sdk_files()
    root = Path(output); sdk.need(not root.exists(), "exclusive fresh FS observation output required"); root.mkdir(parents=True)
    observation = {"schema": "owned_sdk_node_filesystem_observation_v1", "node": profile["node"], "UID": 1641,
                   'evidence_mode': 'linux_original',
                   "account": "dw16", "source_files": profile["source_files"], "sdk_identity_sha256": launch.IDENTITY_SHA,
                   "private_root": str(context.sdk), "module_origins": imported, "imports_fresh": True,
                   "installation_files": profile["installation_files"],
                   "library_files": {str(context.library / name): rec for name, rec in profile["library_files"].items()},
                   "resource_gate": gate, "Native_calls": 0, "FEAs": 0, "training_admission": False}
    from .sdk_production_cluster import _exclusive
    raw_ref = _exclusive(root / "raw_FS_observation.json", sdk.jb(observation))
    return _exclusive(root / "filesystem_proof.json", sdk.jb(dict(observation, raw_observation=raw_ref)))


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True); parser.add_argument("--sha256", required=True)
    parser.add_argument("--source-root", type=Path, required=True); parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--capture-fs", action="store_true")
    args = parser.parse_args(argv)
    if not args.capture_fs: return 0
    sdk.need(launch.record(args.profile)["sha256"] == args.sha256, "FS job profile SHA differs")
    capture({"path": str(args.profile.resolve()), **launch.record(args.profile)}, args.source_root, args.output)
    return 0


if __name__ == "__main__": raise SystemExit(main())
