"""Load and fingerprint declared workloads without changing scientific inputs."""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .models import Artifact, CaseSpec, ResourceSnapshot, WorkloadSpec
from .validation import ValidationError, load_json, restore_toml_nulls

_HASH_BLOCK_SIZE = 1024 * 1024

# Known launch controls, not a dump of the user's full environment.
RUNTIME_ENV_NAMES = (
    "OMP_NUM_THREADS",
    "OMP_PROC_BIND",
    "OMP_PLACES",
    "OMP_DYNAMIC",
    "OMP_NESTED",
    "OMP_MAX_ACTIVE_LEVELS",
    "OMP_THREAD_LIMIT",
    "OMP_SCHEDULE",
    "OMP_STACKSIZE",
    "OMP_WAIT_POLICY",
    "OMP_DEFAULT_DEVICE",
    "OMP_TARGET_OFFLOAD",
    "OMP_NUM_TEAMS",
    "OMP_TEAMS_THREAD_LIMIT",
    "GOMP_CPU_AFFINITY",
    "GOMP_SPINCOUNT",
    "GOMP_STACKSIZE",
    "KMP_AFFINITY",
    "KMP_HW_SUBSET",
    "KMP_BLOCKTIME",
    "KMP_SETTINGS",
    "KMP_STACKSIZE",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "MKL_DYNAMIC",
    "BLIS_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)


def runtime_environment(environ: Mapping[str, str], fixed: Mapping[str, str]) -> dict:
    effective = {**environ, **fixed}
    return {name: effective.get(name) for name in RUNTIME_ENV_NAMES}


@dataclass(frozen=True)
class LoadedWorkload:
    """A checked workload plus the exact identities used for later trials."""

    spec: WorkloadSpec
    manifest_path: Path
    artifact_paths: tuple[tuple[str, Path], ...]
    workload_fingerprint: str
    relevant_environment: tuple[tuple[str, str], ...]
    untracked_external_dependencies: tuple[str, ...]

    def case(self, case_id: str) -> CaseSpec:
        for case in self.spec.cases:
            if case.id == case_id:
                return case
        raise ValidationError(f"workload.cases: unknown case ID {case_id!r}")


def sha256_file(path: str | Path) -> str:
    """Hash a regular file with bounded memory."""
    source = Path(path)
    digest = hashlib.sha256()
    try:
        with source.open("rb") as stream:
            for block in iter(lambda: stream.read(_HASH_BLOCK_SIZE), b""):
                digest.update(block)
    except OSError as exc:
        raise ValidationError(f"cannot hash declared file {source}: {exc}") from exc
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValidationError(f"cannot read workload manifest {path}: {exc}") from exc
    if path.suffix.lower() == ".json":
        data = load_json(text)
    elif path.suffix.lower() == ".toml":
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ValidationError(f"TOML: {exc}") from exc
    else:
        raise ValidationError("workload manifest must use .toml or .json")
    if type(data) is not dict:
        raise ValidationError("workload manifest: expected object")
    return data


def _resolve_from(base: Path, declared: str, field: str) -> Path:
    if not isinstance(declared, str) or not declared or "\0" in declared:
        raise ValidationError(f"{field}: expected a nonempty NUL-free path")
    candidate = Path(declared)
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        return candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValidationError(f"{field}: cannot resolve {declared!r}: {exc}") from exc


def _normalize_paths(data: dict[str, Any], manifest_path: Path) -> dict[str, Any]:
    """Resolve execution paths before the strict WorkloadSpec decoder runs."""
    normalized = dict(data)
    declared_base = normalized.get("base_directory")
    base = _resolve_from(manifest_path.parent, declared_base, "workload.base_directory")
    if not base.is_dir():
        raise ValidationError(f"workload.base_directory: not a directory: {base}")
    normalized["base_directory"] = str(base)

    declared_executable = normalized.get("resolved_executable")
    executable = _resolve_from(base, declared_executable, "workload.resolved_executable")
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValidationError(f"workload.resolved_executable: not an executable file: {executable}")
    normalized["resolved_executable"] = str(executable)

    template = normalized.get("argv_template")
    if type(template) is list and template and isinstance(template[0], str):
        template_executable = _resolve_from(base, template[0], "workload.argv_template[0]")
        if template_executable != executable:
            raise ValidationError(
                "workload.argv_template[0]: executable does not match resolved_executable"
            )
        normalized["argv_template"] = [str(executable), *template[1:]]
    return normalized


def _artifact_path(base: Path, artifact: Artifact, field: str) -> Path:
    path = _resolve_from(base, artifact.path, field)
    if not path.is_file():
        raise ValidationError(f"{field}: declared artifact is not a regular file: {path}")
    actual = sha256_file(path)
    if actual != artifact.sha256:
        raise ValidationError(
            f"{field}: SHA-256 changed for {artifact.id!r}; "
            f"expected {artifact.sha256}, observed {actual}"
        )
    return path


def _check_identities(spec: WorkloadSpec) -> tuple[tuple[str, Path], ...]:
    executable = Path(spec.resolved_executable)
    actual_executable = sha256_file(executable)
    if actual_executable != spec.executable_sha256:
        raise ValidationError(
            "workload.executable_sha256: executable identity changed; "
            f"expected {spec.executable_sha256}, observed {actual_executable}"
        )
    base = Path(spec.base_directory)
    identities: list[tuple[str, Path]] = [("executable", executable)]
    groups = [("workload.artifacts", "workload", spec.artifacts)]
    groups.extend(
        (
            f"workload.cases[{case.id}].input_artifacts",
            f"case:{case.id}",
            case.input_artifacts,
        )
        for case in spec.cases
    )
    for prefix, identity_scope, artifacts in groups:
        for artifact in artifacts:
            identities.append(
                (
                    f"{identity_scope}:{artifact.id}",
                    _artifact_path(base, artifact, f"{prefix}[{artifact.id}].path"),
                )
            )
    return tuple(identities)


def load_workload(path: str | Path, *, environ: Mapping[str, str] | None = None) -> LoadedWorkload:
    """Load a strict TOML/JSON workload and verify every declared file identity.

    Relative base directories are resolved from the manifest directory. Relative
    executables and artifacts are then resolved from that base, never from the
    caller's current directory. Hashes are declarations, not automatically
    refreshed: a changed executable or input is rejected.
    """
    try:
        manifest_path = Path(path).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValidationError(f"cannot resolve workload manifest {path}: {exc}") from exc
    raw = _normalize_paths(_read_manifest(manifest_path), manifest_path)
    if manifest_path.suffix.lower() == ".toml":
        raw = restore_toml_nulls(WorkloadSpec, raw)
    spec = WorkloadSpec.from_dict(raw)
    artifacts = _check_identities(spec)
    if environ is not None and not isinstance(environ, Mapping):
        raise ValidationError("environ must be a string mapping when supplied")
    # fixed_env is the complete declared environment surface for workload identity.
    # Its values are fixed overrides, not names to copy implicitly from the parent.
    relevant = tuple(sorted(spec.fixed_env.items()))
    fingerprint = _canonical_sha256(
        {
            "workload": spec.to_dict(),
            "resolved_artifacts": [
                {"id": identity, "path": str(artifact_path)}
                for identity, artifact_path in artifacts
            ],
            "declared_environment": dict(relevant),
        }
    )
    return LoadedWorkload(
        spec=spec,
        manifest_path=manifest_path,
        artifact_paths=artifacts,
        workload_fingerprint=fingerprint,
        relevant_environment=relevant,
        untracked_external_dependencies=(
            "undeclared files and dynamically loaded libraries are not fingerprinted",
            "container images, services, network state, and accelerator state are not tracked",
            "ambient environment variables outside fixed_env are inherited but not recorded",
        ),
    )


def load_case(path: str | Path, *, base_directory: str | Path) -> CaseSpec:
    """Load one standalone JSON case and verify its declared input artifacts."""
    case_path = Path(path).resolve(strict=True)
    if case_path.suffix.lower() != ".json":
        raise ValidationError("standalone case files must use .json")
    case = CaseSpec.from_dict(load_json(case_path.read_text(encoding="utf-8")))
    base = Path(base_directory).resolve(strict=True)
    for artifact in case.input_artifacts:
        _artifact_path(base, artifact, f"case[{case.id}].input_artifacts[{artifact.id}].path")
    return case


def verify_workload_identity(workload: LoadedWorkload) -> None:
    """Re-hash executable and declared artifacts at the execution boundary."""
    current = _check_identities(workload.spec)
    if current != workload.artifact_paths:
        raise ValidationError("resolved workload artifact paths changed after manifest loading")


def expand_trial_argv(spec: WorkloadSpec, case: CaseSpec, trial_directory: Path) -> list[str]:
    """Expand only declared whole arguments, without recursive substitution."""
    if spec.extensions.get("simtune.literal_argv", False):
        return list(spec.argv_template)
    replacements = {f"{{case.{key}}}": value for key, value in case.arguments.items()}
    replacements["{trial_dir}"] = str(trial_directory)
    argv = [replacements.get(argument, argument) for argument in spec.argv_template]
    if argv[0] != spec.resolved_executable:
        raise ValidationError("workload argv executable changed during substitution")
    return argv


def environment_fingerprint(
    workload: LoadedWorkload,
    snapshot: ResourceSnapshot,
    capabilities: Mapping[str, Any],
) -> str:
    """Fingerprint only declared runtime/resource/tool context, never the full env."""
    tools = capabilities.get("tools", {})
    tool_identities = {
        name: {
            "available": bool(value.get("available", value.get("path"))),
            "path": value.get("path"),
            "version": value.get("version"),
        }
        for name, value in tools.items()
        if isinstance(name, str) and isinstance(value, Mapping)
    }
    return _canonical_sha256(
        {
            "runtime": workload.spec.runtime.to_dict(),
            "fixed_env": dict(workload.relevant_environment),
            "resource_snapshot": snapshot.to_dict(),
            "tools": tool_identities,
        }
    )
