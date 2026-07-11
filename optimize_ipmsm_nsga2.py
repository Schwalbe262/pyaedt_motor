"""Run deterministic nested-control IPMSM optimization with optional pymoo.

The CLI can validate/dry-run a JSON specification without installing pymoo.
Production optimization uses a quality-gated bundle through ``--model-dir``.
``--predictor module:attribute`` remains an explicitly unverified testing
escape hatch; its callable contract is defined in
``ipmsm_optimization.SurrogatePredictor``.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
import csv
import hashlib
import importlib
import io
import json
import math
import os
from pathlib import Path
import pickle
import platform
import socket
import sys
import tempfile
import uuid
from typing import Any, Iterable, Mapping, Sequence

from atomic_publish import (
    PublishReceipt,
    cleanup_publish_receipt,
    publish_no_replace,
    recover_owned_output,
    rollback_owned_output,
)
from ipmsm_optimization import (
    BETA_CONVENTION,
    OptimizationCandidate,
    OptimizationSpec,
    OptimizationSpecError,
    SeedParameterProvider,
    SurrogatePredictor,
    evaluate_design_candidate,
    geometry_metrics,
    load_optimization_spec,
    nondominated_candidates,
    select_validation_candidates,
)
from ipmsm_surrogate_bundle import (
    IPMSMV2SurrogateBundle,
    SurrogateBundleError,
    load_surrogate_bundle,
)


DEFAULT_PARETO_NAME = "pareto.csv"
DEFAULT_FEA_CASES_NAME = "fea_validation_cases.csv"
FEA_DATASET_SCHEMA_VERSION = "ipmsm_v2"
FEA_MODEL_EXTENT = "full_360"
REFERENCE_FEA_QUALITY_PROFILE = "reference_ultra"
STRICT_BUNDLE_VERIFICATION = "STRICT_V2_FINGERPRINT_VERIFIED"
CUSTOM_PREDICTOR_VERIFICATION = "UNVERIFIED_CUSTOM_PREDICTOR"
CHECKPOINT_SCHEMA_VERSION = 1
CHECKPOINT_MANIFEST_NAME = "manifest.json"
CHECKPOINT_CLAIM_NAME = "run.claim.json"
CHECKPOINT_CLAIM_GUARD_NAME = ".run.claim.guard"
CHECKPOINT_MAGIC = b"IPMSM_NS2_CHECKPOINT_V1\n"
CHECKPOINT_SOURCE_FILES = (
    "optimize_ipmsm_nsga2.py",
    "ipmsm_optimization.py",
    "ipmsm_surrogate_bundle.py",
)
PAIR_STAGE_MARKER = ".ipmsm-pair-"
OPTIMIZATION_RUN_ID_FIELD = "optimization_run_id"
PARETO_SHA256_FIELD = "pareto_sha256"
OPTIMIZATION_SPEC_SHA256_FIELD = "optimization_spec_sha256"
SURROGATE_METADATA_SHA256_FIELD = "surrogate_metadata_sha256"
SURROGATE_MODEL_ARTIFACTS_SHA256_FIELD = "surrogate_model_artifacts_sha256"
SURROGATE_VERIFICATION_FIELD = "surrogate_verification"
FEA_PROVENANCE_FIELDS = (
    OPTIMIZATION_RUN_ID_FIELD,
    PARETO_SHA256_FIELD,
    OPTIMIZATION_SPEC_SHA256_FIELD,
    SURROGATE_METADATA_SHA256_FIELD,
    SURROGATE_MODEL_ARTIFACTS_SHA256_FIELD,
    SURROGATE_VERIFICATION_FIELD,
)
OPTIMIZATION_RUN_ID_PREFIX = "ipmsm-optimization-run:sha256:"
UNVERIFIED_PROVENANCE_VALUE = "UNVERIFIED"
DIRECT_EXPORT_VERIFICATION = "UNVERIFIED_DIRECT_EXPORT"


def candidate_design_hash(candidate: OptimizationCandidate) -> str:
    encoded = json.dumps(
        {key: float(value) for key, value in sorted(candidate.design.items())},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_write_bytes(
    path: str | Path,
    payload: bytes,
    *,
    fresh_only: bool = False,
) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if fresh_only and output.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {output}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if fresh_only:
            # This publish is atomic and never replaces an output that appeared
            # after the initial existence check (unlike os.replace).
            publish_no_replace(temporary, output)
        else:
            os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()
    return output


def _atomic_write_json(
    path: str | Path,
    payload: Mapping[str, Any],
    *,
    fresh_only: bool = False,
) -> Path:
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    return _atomic_write_bytes(path, encoded, fresh_only=fresh_only)


def _read_json_object(path: str | Path, description: str) -> Mapping[str, Any]:
    source = Path(path)
    try:
        decoded = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"cannot read {description} {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"invalid {description} JSON {source}: line {exc.lineno}, column {exc.colno}"
        ) from exc
    if not isinstance(decoded, Mapping):
        raise RuntimeError(f"{description} {source} must contain a JSON object")
    return decoded


def _module_version(name: str) -> str:
    try:
        module = importlib.import_module(name)
    except Exception as exc:
        raise RuntimeError(f"cannot import {name} for checkpoint identity: {exc}") from exc
    version = getattr(module, "__version__", None)
    if not isinstance(version, str) or not version:
        raise RuntimeError(f"{name}.__version__ is unavailable for checkpoint identity")
    return version


def _bundle_artifact_hashes(bundle: IPMSMV2SurrogateBundle) -> dict[str, str]:
    model_paths = bundle.metadata.get("model_paths")
    if not isinstance(model_paths, Mapping):
        raise SurrogateBundleError("metadata.model_paths must be an object")
    root = bundle.model_dir.resolve()
    hashes: dict[str, str] = {}
    for target in sorted(model_paths):
        recorded = model_paths[target]
        if isinstance(recorded, str):
            values = [recorded]
        elif isinstance(recorded, Sequence) and not isinstance(recorded, (str, bytes)):
            values = list(recorded)
        else:
            raise SurrogateBundleError(
                f"metadata.model_paths.{target} must be a model path or an array of model paths"
            )
        if not values or any(not isinstance(value, str) or not value for value in values):
            raise SurrogateBundleError(
                f"metadata.model_paths.{target} must contain nonempty model paths"
            )
        for index, value in enumerate(values):
            artifact = root / Path(value).name
            if not artifact.is_file():
                raise SurrogateBundleError(f"model artifact is missing for {target}: {artifact}")
            hashes[f"{target}[{index}]::{artifact.name}"] = _sha256_file(artifact)
    return hashes


def _canonical_model_artifact_sha256(bundle: IPMSMV2SurrogateBundle) -> str:
    return _canonical_json_sha256(_bundle_artifact_hashes(bundle))


def build_surrogate_provenance_context(
    spec_path: str | Path,
    bundle: IPMSMV2SurrogateBundle,
) -> dict[str, str]:
    return {
        OPTIMIZATION_SPEC_SHA256_FIELD: _sha256_file(spec_path),
        SURROGATE_METADATA_SHA256_FIELD: _sha256_file(
            bundle.model_dir.resolve() / "metadata.json"
        ),
        SURROGATE_MODEL_ARTIFACTS_SHA256_FIELD: _canonical_model_artifact_sha256(
            bundle
        ),
        SURROGATE_VERIFICATION_FIELD: STRICT_BUNDLE_VERIFICATION,
    }


def build_custom_predictor_provenance_context(spec_path: str | Path) -> dict[str, str]:
    return {
        OPTIMIZATION_SPEC_SHA256_FIELD: _sha256_file(spec_path),
        SURROGATE_METADATA_SHA256_FIELD: UNVERIFIED_PROVENANCE_VALUE,
        SURROGATE_MODEL_ARTIFACTS_SHA256_FIELD: UNVERIFIED_PROVENANCE_VALUE,
        SURROGATE_VERIFICATION_FIELD: CUSTOM_PREDICTOR_VERIFICATION,
    }


def build_optimization_run_provenance(
    pareto_payload: bytes,
    context: Mapping[str, str] | None,
) -> dict[str, str]:
    base = dict(context or {})
    if not base:
        base = {
            OPTIMIZATION_SPEC_SHA256_FIELD: UNVERIFIED_PROVENANCE_VALUE,
            SURROGATE_METADATA_SHA256_FIELD: UNVERIFIED_PROVENANCE_VALUE,
            SURROGATE_MODEL_ARTIFACTS_SHA256_FIELD: UNVERIFIED_PROVENANCE_VALUE,
            SURROGATE_VERIFICATION_FIELD: DIRECT_EXPORT_VERIFICATION,
        }
    required_context = (
        OPTIMIZATION_SPEC_SHA256_FIELD,
        SURROGATE_METADATA_SHA256_FIELD,
        SURROGATE_MODEL_ARTIFACTS_SHA256_FIELD,
        SURROGATE_VERIFICATION_FIELD,
    )
    missing = [name for name in required_context if not base.get(name)]
    if missing:
        raise ValueError(f"optimization provenance context is missing fields: {missing}")
    provenance = {
        PARETO_SHA256_FIELD: hashlib.sha256(pareto_payload).hexdigest(),
        **{name: str(base[name]) for name in required_context},
    }
    provenance[OPTIMIZATION_RUN_ID_FIELD] = (
        OPTIMIZATION_RUN_ID_PREFIX + _canonical_json_sha256(provenance)
    )
    return provenance


def _normalized_fea_provenance(
    provenance: Mapping[str, str] | None,
) -> dict[str, str]:
    if provenance is None:
        return {name: UNVERIFIED_PROVENANCE_VALUE for name in FEA_PROVENANCE_FIELDS} | {
            SURROGATE_VERIFICATION_FIELD: DIRECT_EXPORT_VERIFICATION,
            OPTIMIZATION_RUN_ID_FIELD: DIRECT_EXPORT_VERIFICATION,
        }
    missing = [name for name in FEA_PROVENANCE_FIELDS if not provenance.get(name)]
    if missing:
        raise ValueError(f"FEA provenance is missing fields: {missing}")
    return {name: str(provenance[name]) for name in FEA_PROVENANCE_FIELDS}


def build_checkpoint_identity(
    spec_path: str | Path,
    bundle: IPMSMV2SurrogateBundle,
    *,
    seeds: Sequence[int],
    population_size: int,
    max_generations: int,
) -> dict[str, Any]:
    """Build the exact run identity required before checkpoint reuse."""

    source_root = Path(__file__).resolve().parent
    metadata_path = bundle.model_dir.resolve() / "metadata.json"
    model_artifact_hashes = _bundle_artifact_hashes(bundle)
    return {
        "spec_sha256": _sha256_file(spec_path),
        "surrogate_bundle": {
            "metadata_sha256": _sha256_file(metadata_path),
            "model_artifact_sha256": model_artifact_hashes,
            "model_artifacts_canonical_sha256": _canonical_json_sha256(
                model_artifact_hashes
            ),
        },
        "optimizer": {
            "seeds": list(seeds),
            "population_size": int(population_size),
            "max_generations": int(max_generations),
        },
        "source_sha256": {
            name: _sha256_file(source_root / name)
            for name in CHECKPOINT_SOURCE_FILES
        },
        "versions": {
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "numpy": _module_version("numpy"),
            "pymoo": _module_version("pymoo"),
            "lightgbm": _module_version("lightgbm"),
        },
    }


def _identity_differences(expected: Any, actual: Any, path: str = "identity") -> list[str]:
    if isinstance(expected, Mapping) and isinstance(actual, Mapping):
        differences: list[str] = []
        for key in sorted(set(expected) | set(actual), key=str):
            child = f"{path}.{key}"
            if key not in expected or key not in actual:
                differences.append(child)
            else:
                differences.extend(_identity_differences(expected[key], actual[key], child))
        return differences
    return [] if expected == actual else [path]


def prepare_checkpoint_directory(
    path: str | Path,
    identity: Mapping[str, Any],
    *,
    resume: bool,
) -> Path:
    root = Path(path)
    manifest_path = root / CHECKPOINT_MANIFEST_NAME
    if resume:
        if not root.is_dir():
            raise RuntimeError(f"checkpoint directory does not exist for --resume: {root}")
        manifest = _read_json_object(manifest_path, "checkpoint manifest")
        if manifest.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            raise RuntimeError(
                "checkpoint manifest schema_version must be "
                f"{CHECKPOINT_SCHEMA_VERSION}; got {manifest.get('schema_version')!r}"
            )
        recorded_identity = manifest.get("identity")
        differences = _identity_differences(identity, recorded_identity)
        if differences:
            raise RuntimeError(
                "checkpoint resume identity mismatch: " + ", ".join(differences[:20])
            )
        return root

    if root.exists():
        if not root.is_dir():
            raise RuntimeError(f"checkpoint path is not a directory: {root}")
        if any(root.iterdir()):
            raise RuntimeError(
                f"checkpoint directory is not empty; use --resume or a fresh directory: {root}"
            )
    else:
        root.mkdir(parents=True)
    _atomic_write_json(
        manifest_path,
        {"schema_version": CHECKPOINT_SCHEMA_VERSION, "identity": dict(identity)},
        fresh_only=True,
    )
    return root


def _write_exclusive_json(path: str | Path, payload: Mapping[str, Any]) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        output,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            output.unlink()
        except OSError:
            pass
        raise
    return output


@contextmanager
def _checkpoint_claim_guard(root: str | Path):
    guard_path = Path(root) / CHECKPOINT_CLAIM_GUARD_NAME
    descriptor = os.open(guard_path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = True
        yield
    finally:
        if locked:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _pid_is_active(pid: int) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        # Python implements os.kill(pid, 0) with TerminateProcess on Windows.
        # Query the process handle instead so a liveness probe can never stop
        # the optimization process it is trying to protect.
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        handle = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:  # ERROR_INVALID_PARAMETER means there is no such PID.
                return False
            # Access denied or an unknown query failure is active/indeterminate.
            return True
        try:
            exit_code = wintypes.DWORD()
            if not get_exit_code(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Unknown platform-specific errors are active/indeterminate, never stale.
        return True
    return True


class CheckpointRunClaim:
    def __init__(self, root: Path, owner_token: str) -> None:
        self.root = root
        self.path = root / CHECKPOINT_CLAIM_NAME
        self.owner_token = owner_token
        self.released = False

    def release(self) -> None:
        if self.released:
            return
        with _checkpoint_claim_guard(self.root):
            record = _read_json_object(self.path, "checkpoint run claim")
            if record.get("owner_token") != self.owner_token:
                raise RuntimeError(
                    "checkpoint run claim ownership changed; refusing release"
                )
            self.path.unlink()
            self.released = True

    def __enter__(self) -> "CheckpointRunClaim":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


def acquire_checkpoint_run_claim(
    root: str | Path,
    identity: Mapping[str, Any],
    *,
    resume: bool,
) -> CheckpointRunClaim:
    checkpoint_root = Path(root)
    claim_path = checkpoint_root / CHECKPOINT_CLAIM_NAME
    identity_sha256 = _canonical_json_sha256(identity)
    owner_token = uuid.uuid4().hex
    owner_host = socket.gethostname()
    owner_pid = os.getpid()
    claim_payload = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "identity_sha256": identity_sha256,
        "owner_pid": owner_pid,
        "owner_host": owner_host,
        "owner_token": owner_token,
    }
    with _checkpoint_claim_guard(checkpoint_root):
        try:
            _write_exclusive_json(claim_path, claim_payload)
        except FileExistsError:
            if not resume:
                raise RuntimeError(
                    f"checkpoint run claim already exists: {claim_path}"
                ) from None
            existing = _read_json_object(claim_path, "checkpoint run claim")
            if existing.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
                raise RuntimeError("checkpoint run claim schema mismatch")
            if existing.get("identity_sha256") != identity_sha256:
                raise RuntimeError(
                    "stale checkpoint claim identity mismatch; refusing recovery"
                )
            existing_host = existing.get("owner_host")
            existing_pid = existing.get("owner_pid")
            if not isinstance(existing_host, str) or not existing_host:
                raise RuntimeError("checkpoint run claim owner_host is invalid")
            if isinstance(existing_pid, bool) or not isinstance(existing_pid, int) or existing_pid <= 0:
                raise RuntimeError("checkpoint run claim owner_pid is invalid")
            if existing_host != owner_host:
                raise RuntimeError(
                    "checkpoint run claim owner is on another host; activity is indeterminate"
                )
            if _pid_is_active(existing_pid):
                raise RuntimeError(
                    f"checkpoint run is already active on {existing_host} pid {existing_pid}"
                )
            claim_path.unlink()
            _write_exclusive_json(claim_path, claim_payload)
    return CheckpointRunClaim(checkpoint_root, owner_token)


def pymoo_dependency_status() -> dict[str, Any]:
    """Return a small, JSON-safe optional dependency report."""

    try:
        module = importlib.import_module("pymoo")
    except Exception as exc:
        return {"pymoo_available": False, "pymoo_version": None, "error": str(exc)}
    return {
        "pymoo_available": True,
        "pymoo_version": getattr(module, "__version__", "unknown"),
        "error": None,
    }


def _load_pymoo_components() -> dict[str, Any]:
    try:
        from pymoo.algorithms.moo.nsga2 import NSGA2
        from pymoo.core.problem import ElementwiseProblem
        from pymoo.operators.crossover.sbx import SBX
        from pymoo.operators.mutation.pm import PM
        from pymoo.operators.sampling.rnd import FloatRandomSampling
        from pymoo.optimize import minimize
    except Exception as exc:
        raise RuntimeError(
            "pymoo is required for optimization; install pymoo>=0.6.2 or use --dry-run"
        ) from exc
    return {
        "NSGA2": NSGA2,
        "ElementwiseProblem": ElementwiseProblem,
        "SBX": SBX,
        "PM": PM,
        "FloatRandomSampling": FloatRandomSampling,
        "minimize": minimize,
    }


def load_predictor(reference: str) -> SurrogatePredictor:
    """Load an unverified testing surrogate from ``module:attribute``."""

    if ":" not in reference:
        raise ValueError("predictor must use module:attribute syntax")
    module_name, attribute_name = reference.split(":", 1)
    if not module_name or not attribute_name:
        raise ValueError("predictor must use module:attribute syntax")
    module = importlib.import_module(module_name)
    predictor = getattr(module, attribute_name)
    if not callable(predictor) and not hasattr(predictor, "predict_one"):
        raise TypeError(f"predictor {reference!r} is not callable and has no predict_one method")
    return predictor


def validate_production_surrogate(
    bundle: IPMSMV2SurrogateBundle,
    spec: OptimizationSpec,
    *,
    quality_profile: str,
) -> None:
    """Cross-check strict model provenance against the spec and FEA contract."""

    if quality_profile != REFERENCE_FEA_QUALITY_PROFILE:
        raise SurrogateBundleError(
            "--fea-quality-profile must be "
            f"{REFERENCE_FEA_QUALITY_PROFILE!r} when --model-dir is used; got {quality_profile!r}"
        )
    bundle.assert_fingerprint_compatible(
        {
            "input_dataset_schema_version": FEA_DATASET_SCHEMA_VERSION,
            "input_beta_calibration_id": spec.beta_calibration.calibration_id,
            "input_beta_convention": spec.beta_calibration.convention,
            "input_model_extent": FEA_MODEL_EXTENT,
            "input_quality_profile": REFERENCE_FEA_QUALITY_PROFILE,
        }
    )


def _design_from_vector(spec: OptimizationSpec, vector: Sequence[float]) -> dict[str, float]:
    if len(vector) != len(spec.design_space):
        raise ValueError("optimizer vector length does not match design_space")
    return {bound.name: float(value) for bound, value in zip(spec.design_space, vector)}


def _seed_checkpoint_path(root: str | Path, seed: int) -> Path:
    return Path(root) / f"seed_{seed}.checkpoint"


def _seed_progress_path(root: str | Path, seed: int) -> Path:
    return Path(root) / f"seed_{seed}.progress.json"


def _completed_generations(algorithm: Any) -> int:
    n_iter = getattr(algorithm, "n_iter", None)
    if isinstance(n_iter, bool) or not isinstance(n_iter, int) or n_iter < 1:
        raise RuntimeError(f"checkpoint algorithm has invalid n_iter: {n_iter!r}")
    return n_iter - 1


def _write_algorithm_checkpoint(
    path: str | Path,
    algorithm: Any,
    *,
    seed: int,
    population_size: int,
    max_generations: int,
) -> str:
    problem = getattr(algorithm, "problem", None)
    if problem is None:
        raise RuntimeError("cannot checkpoint an algorithm without an attached problem")
    algorithm.problem = None
    try:
        payload = pickle.dumps(
            {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "seed": seed,
                "population_size": population_size,
                "max_generations": max_generations,
                "completed_generations": _completed_generations(algorithm),
                "algorithm": algorithm,
            },
            protocol=pickle.HIGHEST_PROTOCOL,
        )
    except Exception as exc:
        raise RuntimeError(f"cannot serialize NSGA-II checkpoint: {exc}") from exc
    finally:
        algorithm.problem = problem
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    envelope = CHECKPOINT_MAGIC + payload_sha256.encode("ascii") + b"\n" + payload
    _atomic_write_bytes(path, envelope)
    return payload_sha256


def _read_algorithm_checkpoint(
    path: str | Path,
    *,
    seed: int,
    population_size: int,
    max_generations: int,
) -> tuple[Any, str]:
    source = Path(path)
    try:
        envelope = source.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"cannot read NSGA-II checkpoint {source}: {exc}") from exc
    if not envelope.startswith(CHECKPOINT_MAGIC):
        raise RuntimeError(f"invalid or partial NSGA-II checkpoint header: {source}")
    remainder = envelope[len(CHECKPOINT_MAGIC):]
    try:
        recorded_digest, payload = remainder.split(b"\n", 1)
    except ValueError as exc:
        raise RuntimeError(f"invalid or partial NSGA-II checkpoint digest: {source}") from exc
    actual_digest = hashlib.sha256(payload).hexdigest()
    try:
        digest_text = recorded_digest.decode("ascii")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"invalid NSGA-II checkpoint digest encoding: {source}") from exc
    if len(digest_text) != 64 or digest_text != actual_digest:
        raise RuntimeError(f"NSGA-II checkpoint checksum mismatch: {source}")
    try:
        # Checkpoints are trusted local run artifacts.  Never accept one from
        # an untrusted source because pickle loading can execute code.
        record = pickle.loads(payload)
    except Exception as exc:
        raise RuntimeError(f"cannot deserialize NSGA-II checkpoint {source}: {exc}") from exc
    if not isinstance(record, Mapping):
        raise RuntimeError(f"NSGA-II checkpoint payload must be an object: {source}")
    expected = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "seed": seed,
        "population_size": population_size,
        "max_generations": max_generations,
    }
    mismatches = [
        f"{name}: expected={value!r}, actual={record.get(name)!r}"
        for name, value in expected.items()
        if record.get(name) != value
    ]
    if mismatches:
        raise RuntimeError("NSGA-II checkpoint identity mismatch: " + "; ".join(mismatches))
    algorithm = record.get("algorithm")
    if algorithm is None or getattr(algorithm, "problem", object()) is not None:
        raise RuntimeError(f"NSGA-II checkpoint algorithm state is invalid: {source}")
    completed = _completed_generations(algorithm)
    if record.get("completed_generations") != completed or completed > max_generations:
        raise RuntimeError(f"NSGA-II checkpoint generation state is invalid: {source}")
    return algorithm, actual_digest


def _write_seed_progress(
    path: str | Path,
    *,
    seed: int,
    algorithm: Any,
    max_generations: int,
    checkpoint_sha256: str,
) -> Path:
    completed = _completed_generations(algorithm)
    return _atomic_write_json(
        path,
        {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "seed": seed,
            "status": "completed" if completed >= max_generations else "running",
            "completed_generations": completed,
            "max_generations": max_generations,
            "n_eval": int(algorithm.evaluator.n_eval),
            "checkpoint_sha256": checkpoint_sha256,
        },
    )


def _run_checkpointed_algorithm(
    problem: Any,
    algorithm: Any,
    *,
    seed: int,
    population_size: int,
    max_generations: int,
    checkpoint_path: str | Path,
    progress_path: str | Path,
    resume: bool,
) -> Any:
    checkpoint = Path(checkpoint_path)
    progress = Path(progress_path)
    checkpoint_sha256: str | None = None
    if resume and checkpoint.is_file():
        algorithm, checkpoint_sha256 = _read_algorithm_checkpoint(
            checkpoint,
            seed=seed,
            population_size=population_size,
            max_generations=max_generations,
        )
        algorithm.problem = problem
        _write_seed_progress(
            progress,
            seed=seed,
            algorithm=algorithm,
            max_generations=max_generations,
            checkpoint_sha256=checkpoint_sha256,
        )
    elif resume:
        if progress.exists():
            raise RuntimeError(
                f"seed {seed} progress exists without its checkpoint: {progress}"
            )
        algorithm.setup(
            problem,
            termination=("n_gen", max_generations),
            seed=seed,
            verbose=False,
            save_history=False,
        )
    else:
        if checkpoint.exists() or progress.exists():
            raise RuntimeError(
                f"seed {seed} checkpoint artifacts already exist; use --resume"
            )
        algorithm.setup(
            problem,
            termination=("n_gen", max_generations),
            seed=seed,
            verbose=False,
            save_history=False,
        )

    while algorithm.has_next():
        algorithm.next()
        checkpoint_sha256 = _write_algorithm_checkpoint(
            checkpoint,
            algorithm,
            seed=seed,
            population_size=population_size,
            max_generations=max_generations,
        )
        _write_seed_progress(
            progress,
            seed=seed,
            algorithm=algorithm,
            max_generations=max_generations,
            checkpoint_sha256=checkpoint_sha256,
        )

    result = algorithm.result()
    result.algorithm = algorithm
    return result


def run_nsga2(
    spec: OptimizationSpec,
    predictor: SurrogatePredictor,
    *,
    seed: int = 42,
    population_size: int | None = None,
    max_generations: int | None = None,
    seed_parameter_provider: SeedParameterProvider | None = None,
    checkpoint_path: str | Path | None = None,
    progress_path: str | Path | None = None,
    resume: bool = False,
) -> list[OptimizationCandidate]:
    """Run one seeded pymoo NSGA-II front and re-evaluate returned designs."""

    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    population = spec.nsga2.population_size if population_size is None else int(population_size)
    generations = spec.nsga2.max_generations if max_generations is None else int(max_generations)
    if population < 2 or generations < 1:
        raise ValueError("population_size must be >= 2 and max_generations must be >= 1")
    if resume and checkpoint_path is None:
        raise ValueError("resume requires checkpoint_path")
    if progress_path is not None and checkpoint_path is None:
        raise ValueError("progress_path requires checkpoint_path")
    if checkpoint_path is not None and progress_path is None:
        progress_path = Path(checkpoint_path).with_suffix(".progress.json")
    if checkpoint_path is not None:
        checkpoint_resolved = os.path.normcase(str(Path(checkpoint_path).resolve()))
        progress_resolved = os.path.normcase(str(Path(progress_path).resolve()))
        if checkpoint_resolved == progress_resolved:
            raise ValueError("checkpoint_path and progress_path must be distinct")

    pymoo = _load_pymoo_components()
    ElementwiseProblem = pymoo["ElementwiseProblem"]
    bounds = spec.design_space

    class IPMSMProblem(ElementwiseProblem):
        def __init__(self) -> None:
            super().__init__(
                n_var=len(bounds),
                n_obj=2,
                n_ieq_constr=len(spec.operating_points),
                xl=[bound.lower for bound in bounds],
                xu=[bound.upper for bound in bounds],
            )

        def _evaluate(self, x: Sequence[float], out: dict[str, Any], *args: Any, **kwargs: Any) -> None:
            design = _design_from_vector(spec, x)
            # Invalid derived geometry should be rejected by constraints, not
            # abort an expensive population evaluation.
            try:
                geometry_metrics(
                    design,
                    design["stack_length_mm"],
                    spec.winding,
                    slot_number=spec.slot_number,
                )
            except ValueError:
                out["F"] = [1e6, 1.0]
                out["G"] = [1e6] * len(spec.operating_points)
                return
            candidate = evaluate_design_candidate(
                design,
                spec,
                predictor,
                seed=seed,
                seed_parameter_provider=seed_parameter_provider,
            )
            out["F"] = list(candidate.objectives)
            out["G"] = list(candidate.constraint_violations)

    algorithm = pymoo["NSGA2"](
        pop_size=population,
        sampling=pymoo["FloatRandomSampling"](),
        crossover=pymoo["SBX"](
            prob=spec.nsga2.crossover_probability,
            eta=spec.nsga2.crossover_eta,
        ),
        mutation=pymoo["PM"](
            prob=1.0 / len(bounds),
            eta=spec.nsga2.mutation_eta,
        ),
        eliminate_duplicates=True,
    )
    problem = IPMSMProblem()
    if checkpoint_path is None:
        result = pymoo["minimize"](
            problem,
            algorithm,
            ("n_gen", generations),
            seed=seed,
            verbose=False,
            save_history=False,
        )
    else:
        result = _run_checkpointed_algorithm(
            problem,
            algorithm,
            seed=seed,
            population_size=population,
            max_generations=generations,
            checkpoint_path=checkpoint_path,
            progress_path=progress_path,
            resume=resume,
        )
    if result.X is None:
        return []
    raw_vectors = result.X.tolist() if hasattr(result.X, "tolist") else result.X
    if raw_vectors and isinstance(raw_vectors[0], (int, float)):
        raw_vectors = [raw_vectors]
    candidates: list[OptimizationCandidate] = []
    for index, vector in enumerate(raw_vectors, start=1):
        design = _design_from_vector(spec, vector)
        try:
            candidate = evaluate_design_candidate(
                design,
                spec,
                predictor,
                candidate_id=f"nsga_s{seed}_{index:04d}",
                seed=seed,
                seed_parameter_provider=seed_parameter_provider,
            )
        except ValueError as exc:
            if "derived geometry" in str(exc) or "slot area" in str(exc):
                continue
            raise
        candidates.append(candidate)
    return nondominated_candidates(candidates)


def run_nsga2_multiseed(
    spec: OptimizationSpec,
    predictor: SurrogatePredictor,
    *,
    seeds: Iterable[int] | None = None,
    population_size: int | None = None,
    max_generations: int | None = None,
    seed_parameter_provider: SeedParameterProvider | None = None,
    checkpoint_dir: str | Path | None = None,
    resume: bool = False,
) -> list[OptimizationCandidate]:
    """Run and merge deterministic fronts, removing duplicate geometries."""

    chosen_seeds = tuple(spec.nsga2.seeds if seeds is None else seeds)
    if not chosen_seeds:
        raise ValueError("at least one NSGA-II seed is required")
    if len(set(chosen_seeds)) != len(chosen_seeds):
        raise ValueError("NSGA-II seeds must not contain duplicates")
    if resume and checkpoint_dir is None:
        raise ValueError("resume requires checkpoint_dir")
    merged: dict[tuple[float, ...], OptimizationCandidate] = {}
    for seed in chosen_seeds:
        checkpoint_path = (
            _seed_checkpoint_path(checkpoint_dir, seed)
            if checkpoint_dir is not None
            else None
        )
        progress_path = (
            _seed_progress_path(checkpoint_dir, seed)
            if checkpoint_dir is not None
            else None
        )
        for candidate in run_nsga2(
            spec,
            predictor,
            seed=seed,
            population_size=population_size,
            max_generations=max_generations,
            seed_parameter_provider=seed_parameter_provider,
            checkpoint_path=checkpoint_path,
            progress_path=progress_path,
            resume=resume,
        ):
            key = tuple(round(candidate.design[bound.name], 10) for bound in spec.design_space)
            existing = merged.get(key)
            if existing is None or candidate.total_constraint_violation < existing.total_constraint_violation:
                merged[key] = candidate
    return nondominated_candidates(merged.values())


def _safe_column_name(name: str) -> str:
    return re_sub_nonword(name)


def re_sub_nonword(value: str) -> str:
    return "".join(character if character.isalnum() or character == "_" else "_" for character in value)


def pareto_fieldnames(spec: OptimizationSpec) -> list[str]:
    fields = [
        "candidate_id",
        "seed",
        "feasible",
        "active_volume_m3",
        "cycle_efficiency",
        "objective_one_minus_cycle_efficiency",
        "phase_resistance_100c_ohm",
        "slot_fill_ratio",
        "total_constraint_violation",
        "max_uncertainty_score",
    ]
    fields.extend(spec.design_variable_names)
    for point in spec.operating_points:
        prefix = _safe_column_name(point.name)
        fields.extend(
            [
                f"{prefix}_speed_rpm",
                f"{prefix}_target_kind",
                f"{prefix}_required_torque_nm",
                f"{prefix}_required_power_w",
                f"{prefix}_current_peak_a",
                f"{prefix}_beta_deg",
                f"{prefix}_id_a",
                f"{prefix}_iq_a",
                f"{prefix}_torque_nm",
                f"{prefix}_torque_lcb_nm",
                f"{prefix}_voltage_peak_ucb_v",
                f"{prefix}_core_loss_ucb_w",
                f"{prefix}_solid_loss_ucb_w",
                f"{prefix}_copper_loss_w",
                f"{prefix}_total_loss_ucb_w",
                f"{prefix}_efficiency",
                f"{prefix}_feasible",
                f"{prefix}_constraint_violation",
            ]
        )
    return fields


def require_fresh_outputs(paths: Iterable[str | Path]) -> None:
    outputs = [Path(path) for path in paths]
    normalized = [os.path.normcase(str(path.resolve())) for path in outputs]
    if len(set(normalized)) != len(normalized):
        raise ValueError("Pareto and FEA final output paths must be distinct")
    existing = [str(path) for path in outputs if path.exists()]
    if existing:
        raise FileExistsError(
            "refusing to overwrite existing final optimization outputs: "
            + ", ".join(existing)
        )


def candidate_to_pareto_row(candidate: OptimizationCandidate, spec: OptimizationSpec) -> dict[str, Any]:
    row: dict[str, Any] = {
        "candidate_id": candidate.candidate_id,
        "seed": "" if candidate.seed is None else candidate.seed,
        "feasible": candidate.feasible,
        "active_volume_m3": candidate.active_volume_m3,
        "cycle_efficiency": candidate.cycle_efficiency,
        "objective_one_minus_cycle_efficiency": 1.0 - candidate.cycle_efficiency,
        "phase_resistance_100c_ohm": candidate.phase_resistance_ohm,
        "slot_fill_ratio": candidate.slot_fill_ratio,
        "total_constraint_violation": candidate.total_constraint_violation,
        "max_uncertainty_score": candidate.max_uncertainty_score,
    }
    row.update(candidate.design)
    control_by_name = {control.operating_point.name: control for control in candidate.control_results}
    for point in spec.operating_points:
        control = control_by_name[point.name]
        prefix = _safe_column_name(point.name)
        row.update(
            {
                f"{prefix}_speed_rpm": point.speed_rpm,
                f"{prefix}_target_kind": point.target_kind,
                f"{prefix}_required_torque_nm": point.required_torque_nm,
                f"{prefix}_required_power_w": point.required_power_w,
                f"{prefix}_current_peak_a": control.current_peak_a,
                f"{prefix}_beta_deg": control.beta_deg,
                f"{prefix}_id_a": control.id_a,
                f"{prefix}_iq_a": control.iq_a,
                f"{prefix}_torque_nm": control.prediction.torque_nm,
                f"{prefix}_torque_lcb_nm": control.prediction.torque_lcb_nm,
                f"{prefix}_voltage_peak_ucb_v": control.prediction.voltage_peak_ucb_v,
                f"{prefix}_core_loss_ucb_w": control.prediction.core_loss_ucb_w,
                f"{prefix}_solid_loss_ucb_w": control.prediction.solid_loss_ucb_w,
                f"{prefix}_copper_loss_w": control.copper_loss_w,
                f"{prefix}_total_loss_ucb_w": control.total_loss_ucb_w,
                f"{prefix}_efficiency": control.efficiency,
                f"{prefix}_feasible": control.feasible,
                f"{prefix}_constraint_violation": control.total_violation,
            }
        )
    return row


def render_pareto_csv_bytes(
    candidates: Iterable[OptimizationCandidate],
    spec: OptimizationSpec,
) -> bytes:
    rows = nondominated_candidates(candidates)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=pareto_fieldnames(spec), extrasaction="ignore")
    writer.writeheader()
    writer.writerows(candidate_to_pareto_row(candidate, spec) for candidate in rows)
    return stream.getvalue().encode("utf-8")


def write_pareto_csv(
    path: str | Path,
    candidates: Iterable[OptimizationCandidate],
    spec: OptimizationSpec,
    *,
    fresh_only: bool = False,
) -> Path:
    output = Path(path)
    return _atomic_write_bytes(
        output,
        render_pareto_csv_bytes(candidates, spec),
        fresh_only=fresh_only,
    )


def fea_case_fieldnames(spec: OptimizationSpec) -> list[str]:
    return [
        "case_id",
        "geometry_group_id",
        "design_hash",
        "doe_split",
        "repeat_of_case_id",
        "dataset_schema_version",
        *FEA_PROVENANCE_FIELDS,
        *[bound.name for bound in spec.geometry_design_space],
        "stack_length_mm",
        "slot_num",
        "pole_num",
        "base_rpm",
        "i_peak_a",
        "beta_dq_deg",
        "beta_convention",
        "electrical_zero_deg",
        "beta_calibration_id",
        "model_extent",
        "symmetry_factor",
        "use_periodic_boundary",
        "phase_resistance_ohm",
        "vdc_v",
        "series_turns_per_phase",
        "turns_per_coil_side",
        "quality_profile",
        "geometry_mode",
        "operation",
        "candidate_id",
        "operating_point_id",
        "control_source",
        "surrogate_torque_lcb_nm",
        "surrogate_voltage_peak_ucb_v",
        "surrogate_total_loss_ucb_w",
    ]


def render_fea_cases_csv_bytes(
    candidates: Iterable[OptimizationCandidate],
    spec: OptimizationSpec,
    *,
    quality_profile: str = "reference_ultra",
    provenance: Mapping[str, str] | None = None,
) -> bytes:
    rows = list(candidates)
    if not rows:
        raise ValueError("FEA validation case export requires at least one feasible Pareto candidate")
    infeasible_ids = [candidate.candidate_id for candidate in rows if not candidate.feasible]
    if infeasible_ids:
        raise ValueError(
            "FEA validation case export refuses infeasible candidates: "
            + ", ".join(infeasible_ids)
        )
    provenance_values = _normalized_fea_provenance(provenance)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fea_case_fieldnames(spec), extrasaction="ignore")
    writer.writeheader()
    for candidate in rows:
        design_hash = candidate_design_hash(candidate)
        for control in candidate.control_results:
            row: dict[str, Any] = dict(candidate.design)
            row.update(
                {
                    "case_id": f"{candidate.candidate_id}__{control.operating_point.name}",
                    "geometry_group_id": f"optimization_{candidate.candidate_id}",
                    "design_hash": design_hash,
                    "doe_split": "test",
                    "repeat_of_case_id": "",
                    "dataset_schema_version": FEA_DATASET_SCHEMA_VERSION,
                    **provenance_values,
                    "slot_num": spec.slot_number,
                    "pole_num": spec.pole_number,
                    "base_rpm": control.operating_point.speed_rpm,
                    "i_peak_a": control.current_peak_a,
                    "beta_dq_deg": control.beta_deg,
                    "beta_convention": BETA_CONVENTION,
                    "electrical_zero_deg": spec.beta_calibration.electrical_zero_deg,
                    "beta_calibration_id": spec.beta_calibration.calibration_id,
                    "model_extent": FEA_MODEL_EXTENT,
                    "symmetry_factor": 1,
                    "use_periodic_boundary": False,
                    "phase_resistance_ohm": candidate.phase_resistance_ohm,
                    "vdc_v": spec.inverter.vdc_v,
                    "series_turns_per_phase": spec.winding.series_turns_per_phase,
                    "turns_per_coil_side": spec.winding.turns_per_coil_side,
                    "quality_profile": quality_profile,
                    "geometry_mode": "fixed",
                    "operation": "sin_current",
                    "candidate_id": candidate.candidate_id,
                    "operating_point_id": control.operating_point.name,
                    "control_source": "surrogate_inner_search",
                    "surrogate_torque_lcb_nm": control.prediction.torque_lcb_nm,
                    "surrogate_voltage_peak_ucb_v": control.prediction.voltage_peak_ucb_v,
                    "surrogate_total_loss_ucb_w": control.total_loss_ucb_w,
                }
            )
            writer.writerow(row)
    return stream.getvalue().encode("utf-8")


def write_fea_cases_csv(
    path: str | Path,
    candidates: Iterable[OptimizationCandidate],
    spec: OptimizationSpec,
    *,
    quality_profile: str = "reference_ultra",
    fresh_only: bool = False,
    provenance: Mapping[str, str] | None = None,
) -> Path:
    output = Path(path)
    return _atomic_write_bytes(
        output,
        render_fea_cases_csv_bytes(
            candidates,
            spec,
            quality_profile=quality_profile,
            provenance=provenance,
        ),
        fresh_only=fresh_only,
    )


def _pair_stage_path(output: str | Path, token: str) -> Path:
    target = Path(output)
    return target.parent / f".{target.name}{PAIR_STAGE_MARKER}{token}.stage"


def _pair_proof_path(output: str | Path, token: str) -> Path:
    return _pair_stage_path(output, token).with_suffix(".stage.proof")


def _pair_stage_tokens(output: str | Path) -> set[str]:
    target = Path(output)
    prefix = f".{target.name}{PAIR_STAGE_MARKER}"
    suffix = ".stage"
    if not target.parent.is_dir():
        return set()
    return {
        item.name[len(prefix):-len(suffix)]
        for item in target.parent.iterdir()
        if item.name.startswith(prefix)
        and item.name.endswith(suffix)
        and len(item.name) > len(prefix) + len(suffix)
    }


def _pair_proof_tokens(output: str | Path) -> set[str]:
    target = Path(output)
    prefix = f".{target.name}{PAIR_STAGE_MARKER}"
    suffix = ".stage.proof"
    if not target.parent.is_dir():
        return set()
    return {
        item.name[len(prefix):-len(suffix)]
        for item in target.parent.iterdir()
        if item.name.startswith(prefix)
        and item.name.endswith(suffix)
        and len(item.name) > len(prefix) + len(suffix)
    }


def _samefile(left: str | Path, right: str | Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _remove_stage(path: str | Path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass


def recover_incomplete_csv_pair(
    pareto_path: str | Path,
    fea_path: str | Path,
) -> bool:
    """Rollback a crash orphan only when its retained FEA stage proves ownership."""

    pareto = Path(pareto_path)
    fea = Path(fea_path)
    if os.path.lexists(pareto) or not os.path.lexists(fea):
        return False
    tokens = _pair_stage_tokens(fea) | _pair_proof_tokens(fea)
    for token in sorted(tokens):
        fea_stage = _pair_stage_path(fea, token)
        fea_proof = _pair_proof_path(fea, token)
        pareto_stage = _pair_stage_path(pareto, token)
        if _samefile(fea_stage, fea):
            fea.unlink()
            _remove_stage(fea_proof)
        elif fea_proof.is_file():
            if not recover_owned_output(fea_proof, fea):
                continue
        else:
            continue
        _remove_stage(fea_stage)
        _remove_stage(pareto_stage)
        return True
    return False


def write_optimization_csv_pair(
    pareto_path: str | Path,
    fea_path: str | Path,
    pareto_candidates: Iterable[OptimizationCandidate],
    fea_candidates: Iterable[OptimizationCandidate],
    spec: OptimizationSpec,
    *,
    quality_profile: str = REFERENCE_FEA_QUALITY_PROFILE,
    provenance_context: Mapping[str, str] | None = None,
) -> tuple[Path, Path, dict[str, str]]:
    """Publish FEA first and Pareto last as the atomic pair commit marker."""

    pareto = Path(pareto_path)
    fea = Path(fea_path)
    pareto_payload = render_pareto_csv_bytes(pareto_candidates, spec)
    provenance = build_optimization_run_provenance(
        pareto_payload,
        provenance_context,
    )
    fea_payload = render_fea_cases_csv_bytes(
        fea_candidates,
        spec,
        quality_profile=quality_profile,
        provenance=provenance,
    )
    require_fresh_outputs((pareto, fea))
    token = uuid.uuid4().hex
    pareto_stage = _pair_stage_path(pareto, token)
    fea_stage = _pair_stage_path(fea, token)
    preserve_stages = False
    fea_receipt: PublishReceipt | None = None
    try:
        _atomic_write_bytes(pareto_stage, pareto_payload, fresh_only=True)
        _atomic_write_bytes(fea_stage, fea_payload, fresh_only=True)
        fea_receipt = publish_no_replace(
            fea_stage,
            fea,
            proof_path=_pair_proof_path(fea, token),
        )
        try:
            publish_no_replace(pareto_stage, pareto)
        except BaseException:
            preserve_stages = not rollback_owned_output(fea_receipt)
            raise
    finally:
        if not preserve_stages:
            if fea_receipt is not None:
                cleanup_publish_receipt(fea_receipt)
            _remove_stage(fea_stage)
            _remove_stage(pareto_stage)
    return pareto, fea, provenance


def dry_run_summary(spec: OptimizationSpec) -> dict[str, Any]:
    dependency = pymoo_dependency_status()
    return {
        "status": "dry_run",
        "schema_version": spec.schema_version,
        "design_variables": [
            {"name": bound.name, "lower": bound.lower, "upper": bound.upper}
            for bound in spec.design_space
        ],
        "operating_points": [
            {
                "name": point.name,
                "speed_rpm": point.speed_rpm,
                "target_kind": point.target_kind,
                "required_torque_nm": point.required_torque_nm,
                "required_power_w": point.required_power_w,
                "duty_weight": point.duty_weight,
            }
            for point in spec.operating_points
        ],
        "phase_peak_voltage_limit_v": spec.phase_peak_voltage_limit_v,
        "inverter_current_limit_a": spec.current_limit_a,
        "current_density_limited_peak_current_a": spec.current_density_limited_peak_current_a,
        "effective_peak_current_limit_a": spec.effective_peak_current_limit_a,
        "beta_bounds_deg": list(spec.beta_bounds_deg),
        "beta_calibration": {
            "convention": spec.beta_calibration.convention,
            "electrical_zero_deg": spec.beta_calibration.electrical_zero_deg,
            "calibration_id": spec.beta_calibration.calibration_id,
        },
        "nsga2": {
            "population_size": spec.nsga2.population_size,
            "max_generations": spec.nsga2.max_generations,
            "seeds": list(spec.nsga2.seeds),
        },
        "dependencies": dependency,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", help="Versioned optimization JSON")
    predictor_group = parser.add_mutually_exclusive_group()
    predictor_group.add_argument(
        "--predictor",
        help="UNVERIFIED testing surrogate as module:attribute; production should use --model-dir",
    )
    predictor_group.add_argument(
        "--model-dir",
        help="Strict train_ipmsm_lightgbm v2 bundle containing metadata.json and models",
    )
    parser.add_argument("--output-dir", default="ipmsm_optimization_output")
    parser.add_argument("--pareto-output")
    parser.add_argument("--fea-cases-output")
    parser.add_argument(
        "--fea-quality-profile",
        default=REFERENCE_FEA_QUALITY_PROFILE,
        help="FEA validation profile; strict --model-dir runs require reference_ultra",
    )
    parser.add_argument("--population-size", type=int)
    parser.add_argument("--max-generations", type=int)
    parser.add_argument("--seed", type=int, action="append", dest="seeds")
    parser.add_argument("--max-fea-candidates", type=int)
    parser.add_argument(
        "--checkpoint-dir",
        help="Opt-in strict --model-dir checkpoint directory, written after every generation",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume an identity-matched --checkpoint-dir run",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--check-dependencies", action="store_true")
    parser.add_argument("--fail-on-missing-dependencies", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.resume and not args.checkpoint_dir:
        parser.error("--resume requires --checkpoint-dir")
    if args.checkpoint_dir and not args.model_dir:
        parser.error("--checkpoint-dir/--resume require production --model-dir")
    if args.checkpoint_dir and args.dry_run:
        parser.error("--checkpoint-dir/--resume cannot be used with --dry-run")
    if args.check_dependencies and not args.spec:
        report = pymoo_dependency_status()
        print(json.dumps(report, sort_keys=True))
        return 1 if args.fail_on_missing_dependencies and not report["pymoo_available"] else 0
    if not args.spec:
        parser.error("--spec is required unless only --check-dependencies is used")
    try:
        spec = load_optimization_spec(args.spec)
        if args.dry_run:
            summary = dry_run_summary(spec)
            if args.model_dir:
                bundle = load_surrogate_bundle(args.model_dir)
                validate_production_surrogate(
                    bundle,
                    spec,
                    quality_profile=args.fea_quality_profile,
                )
                summary["surrogate_bundle"] = bundle.summary()
                summary["surrogate_verification"] = STRICT_BUNDLE_VERIFICATION
            elif args.predictor:
                summary["surrogate_verification"] = CUSTOM_PREDICTOR_VERIFICATION
            print(json.dumps(summary, indent=2, sort_keys=True))
            return 0
        if args.check_dependencies:
            report = pymoo_dependency_status()
            print(json.dumps(report, sort_keys=True))
            if args.fail_on_missing_dependencies and not report["pymoo_available"]:
                return 1
            if not args.predictor and not args.model_dir:
                return 0
        if not args.predictor and not args.model_dir:
            parser.error("one of --model-dir or --predictor is required for optimization")
        chosen_seeds = tuple(spec.nsga2.seeds if args.seeds is None else args.seeds)
        if not chosen_seeds or any(seed < 0 for seed in chosen_seeds):
            raise ValueError("NSGA-II seeds must contain nonnegative integers")
        if len(set(chosen_seeds)) != len(chosen_seeds):
            raise ValueError("NSGA-II seeds must not contain duplicates")
        population_size = (
            spec.nsga2.population_size
            if args.population_size is None
            else args.population_size
        )
        max_generations = (
            spec.nsga2.max_generations
            if args.max_generations is None
            else args.max_generations
        )
        if population_size < 2 or max_generations < 1:
            raise ValueError("population_size must be >= 2 and max_generations must be >= 1")
        output_dir = Path(args.output_dir)
        pareto_path = Path(args.pareto_output) if args.pareto_output else output_dir / DEFAULT_PARETO_NAME
        fea_path = Path(args.fea_cases_output) if args.fea_cases_output else output_dir / DEFAULT_FEA_CASES_NAME
        recovered_incomplete_pair = recover_incomplete_csv_pair(pareto_path, fea_path)
        require_fresh_outputs((pareto_path, fea_path))
        checkpoint_root: Path | None = None
        checkpoint_claim: CheckpointRunClaim | None = None
        if args.model_dir:
            predictor = load_surrogate_bundle(args.model_dir)
            validate_production_surrogate(
                predictor,
                spec,
                quality_profile=args.fea_quality_profile,
            )
            surrogate_verification = STRICT_BUNDLE_VERIFICATION
            provenance_context = build_surrogate_provenance_context(
                args.spec,
                predictor,
            )
            if args.checkpoint_dir:
                identity = build_checkpoint_identity(
                    args.spec,
                    predictor,
                    seeds=chosen_seeds,
                    population_size=population_size,
                    max_generations=max_generations,
                )
                checkpoint_root = prepare_checkpoint_directory(
                    Path(args.checkpoint_dir),
                    identity,
                    resume=args.resume,
                )
                checkpoint_claim = acquire_checkpoint_run_claim(
                    checkpoint_root,
                    identity,
                    resume=args.resume,
                )
        else:
            predictor = load_predictor(args.predictor)
            surrogate_verification = CUSTOM_PREDICTOR_VERIFICATION
            provenance_context = build_custom_predictor_provenance_context(args.spec)
        with checkpoint_claim if checkpoint_claim is not None else nullcontext():
            candidates = run_nsga2_multiseed(
                spec,
                predictor,
                seeds=chosen_seeds,
                population_size=population_size,
                max_generations=max_generations,
                checkpoint_dir=checkpoint_root,
                resume=args.resume,
            )
            if not candidates:
                raise RuntimeError("NSGA-II returned no evaluable candidates")
            pareto_candidates = nondominated_candidates(candidates)
            feasible_pareto_candidates = [
                candidate for candidate in pareto_candidates if candidate.feasible
            ]
            if not feasible_pareto_candidates:
                raise RuntimeError(
                    "NSGA-II returned zero feasible Pareto candidates; refusing Pareto/FEA export"
                )
            selected = select_validation_candidates(
                pareto_candidates,
                max_candidates=args.max_fea_candidates or spec.nsga2.max_fea_candidates,
            )
            _, _, run_provenance = write_optimization_csv_pair(
                pareto_path,
                fea_path,
                pareto_candidates,
                selected,
                spec,
                quality_profile=args.fea_quality_profile,
                provenance_context=provenance_context,
            )
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "pareto_candidates": len(pareto_candidates),
                        "feasible_pareto_candidates": len(feasible_pareto_candidates),
                        "fea_candidates": len(selected),
                        "fea_cases": sum(len(item.control_results) for item in selected),
                        SURROGATE_VERIFICATION_FIELD: surrogate_verification,
                        OPTIMIZATION_RUN_ID_FIELD: run_provenance[OPTIMIZATION_RUN_ID_FIELD],
                        PARETO_SHA256_FIELD: run_provenance[PARETO_SHA256_FIELD],
                        "checkpoint_dir": "" if checkpoint_root is None else str(checkpoint_root),
                        "resumed": bool(args.resume),
                        "recovered_incomplete_output_pair": recovered_incomplete_pair,
                        "pareto_output": str(pareto_path),
                        "fea_cases_output": str(fea_path),
                    },
                    sort_keys=True,
                )
            )
        return 0
    except (
        OptimizationSpecError,
        SurrogateBundleError,
        ImportError,
        AttributeError,
        TypeError,
        ValueError,
        RuntimeError,
        OSError,
    ) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
