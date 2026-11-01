"""One isolated Python process, ordinary imports of an exact archived source tree.

This worker calls only existing pure source parsers. It never constructs an
AEDT object, patches a module/function, or changes an interpreter-wide hook.
"""
from pathlib import Path
import ast
from dataclasses import asdict
import hashlib
import json
import sys


def require(value, message):
    if not value:
        raise ValueError(message)


def record(path):
    raw = path.read_bytes()
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def main():
    request_path = Path(sys.argv[1])
    require(record(request_path)["sha256"] == sys.argv[2], "numeric worker request changed")
    request = json.loads(request_path.read_bytes())
    root = Path(request["source_root"]).resolve(strict=True)
    for name, expected in request["source_files"].items():
        require(record(root / name) == expected, "numeric source bytes changed: " + name)
    # No caller's import cache is inherited. These are the source tree's normal
    # package names in a fresh -I interpreter, not module replacements.
    sys.path[:0] = [str(root), str(root / "src")]
    import run_ipmsm_batch as runner
    from pyaedt_motor import execution_evidence as source_helpers
    from pyaedt_motor import public_batch_pilot as parser
    from module import ipmsm_ppt_setup as ppt
    require(Path(runner.__file__).resolve() == root / "run_ipmsm_batch.py", "wrong ordinary runner import")
    tree = ast.parse(Path(source_helpers.__file__).read_bytes())
    for name, expected in request["helper_AST"].items():
        node = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name), None)
        require(node is not None and hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest() == expected,
                "unsupported archived pure-helper definition: " + name)
    files = source_helpers.EvidenceFiles(**request["cli_index"])
    contract, case, builder, pilot = [files.json(k) for k in ("contract", "case", "builder_result", "pilot_result")]
    builder = source_helpers._builder_copy(builder, pilot)
    spec = runner.build_spec(case, default_electrical_zero_deg=contract["options"]["electrical_zero_deg"])
    count = source_helpers._verify_production_builder_inputs(runner, case, spec, builder)
    required = [key for key in ppt.PPT_REPORT_DEFS if not (key == "PPT_EnergyError" and not spec.solver_output_error)
                and not (key == "PPT_Inductance_Matrix" and not spec.compute_inductance)]
    numeric = source_helpers._replay_metrics(files, runner, spec, required, builder, pilot["postprocess"],
                                            case.get("operation", "sin_current"))
    actual = parser.saved_native(files.path("project"))
    imports = {}
    for name, module in tuple(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if not filename:
            continue
        path = Path(filename).resolve()
        if path.is_relative_to(root):
            relative = path.relative_to(root).as_posix()
            require(relative in request["source_files"] and record(path) == request["source_files"][relative],
                    "unsealed ordinary local import: " + name)
            imports[name] = {"relative_path": relative, **record(path)}
        elif name == "run_ipmsm_batch" or name == "module" or name.startswith(("module.", "pyaedt_motor.")):
            raise ValueError("local numeric dependency escaped archived tree: " + name)
    for name, expected in request["source_files"].items():
        require(record(root / name) == expected, "numeric source drift after replay: " + name)
    files.assert_unchanged()
    result = {"numeric": numeric, "requested_spec": asdict(spec), "required_reports": required,
              "actual_saved_native": actual, "ordinary_imports": imports,
              "builder_input_fields_verified": count, "Native_calls": 0, "module_or_function_patches": 0}
    with Path(request["output_path"]).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(parser.result_json(result), stream, sort_keys=True, allow_nan=False)
        stream.write("\n")


if __name__ == "__main__":
    main()
