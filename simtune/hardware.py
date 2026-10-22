"""Read-only processor provenance, separate from topology and speed claims."""

from __future__ import annotations

from .models import ResourceSnapshot
from .workloads import _canonical_sha256

HARDWARE_KEY = "simtune.hardware"
CLASS_ALGORITHM = "processor-class-v1"
LIMITATIONS = [
    "A processor class match is evidence context, not a current speed guarantee.",
    "Memory technology, frequencies, firmware, dynamic dependencies and background load remain untracked.",
]


def normalize_architecture(value: str) -> str:
    value = value.strip().lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(value, value)


def processor_identity(cpuinfo: str | None, eligible, architecture, source, reason=None) -> dict:
    """Retain every eligible CPU, including missing/duplicate Linux cpuinfo rows.

    ARM's global Hardware/Processor strings do not identify each CPU. Use its
    per-CPU implementer/part/architecture/variant/revision instead. No first-row
    fallback is permitted, including for heterogeneous hosts.
    """
    architecture = normalize_architecture(architecture) if architecture else None
    rows = {}
    duplicates = set()
    for block in (cpuinfo or "").strip().split("\n\n"):
        values = {}
        for line in block.splitlines():
            key, sep, value = line.partition(":")
            if sep:
                values[key.strip().lower()] = " ".join(value.split())
        try:
            cpu = int(values.get("processor", ""))
        except ValueError:
            continue
        if cpu in rows:
            duplicates.add(cpu)
        rows[cpu] = values
    processors = []
    for cpu in sorted(eligible or []):
        fields = rows.get(cpu, {})
        missing = {}
        vendor = model = None
        attributes = {}
        if architecture in {"x86_64", "i386", "i686"}:
            vendor, model = fields.get("vendor_id"), fields.get("model name")
            attributes = {
                name: fields[name]
                for name in ("cpu family", "model", "stepping")
                if fields.get(name)
            }
        elif architecture in {"aarch64", "armv7l", "armv8l"}:
            vendor = fields.get("cpu implementer")
            names = ("cpu architecture", "cpu part", "cpu variant", "cpu revision")
            attributes = {name: fields[name] for name in names if fields.get(name)}
            if len(attributes) == len(names):
                model = ";".join(f"{name}={attributes[name]}" for name in names)
        else:
            missing["representation"] = "unsupported or unknown processor architecture"
        for name, value in (("vendor", vendor), ("model", model)):
            if not value:
                missing[name] = reason or f"per-CPU {name} identity missing from Linux cpuinfo"
        if cpu in duplicates:
            missing["processor"] = "duplicate processor rows; identity is ambiguous"
        processors.append(
            dict(
                cpu_id=cpu,
                vendor=vendor or None,
                model=model or None,
                attributes=attributes,
                unavailable=missing,
            )
        )
    missing = {}
    if architecture is None:
        missing["architecture"] = "architecture was not observed"
    if not eligible:
        missing["processors"] = "eligible CPU boundary is unknown or empty"
    elif any(p["unavailable"] for p in processors):
        missing["processors"] = "one or more eligible processor identities are incomplete"
    return dict(
        schema_version=1,
        class_algorithm=CLASS_ALGORITHM,
        architecture=architecture,
        source=source,
        processors=processors,
        complete=not missing,
        unavailable=missing,
    )


def hardware_context(snapshot: ResourceSnapshot) -> dict:
    """Recompute the class from recorded observations; never fill old records."""
    data = snapshot.extensions.get(HARDWARE_KEY)
    reasons = []
    normalized = None
    if not isinstance(data, dict):
        reasons.append("record has no hardware identity extension; explicit revalidation required")
    elif (
        type(data.get("schema_version")) is not int
        or data["schema_version"] != 1
        or data.get("class_algorithm") != CLASS_ALGORITHM
    ):
        reasons.append("unsupported hardware identity version/algorithm")
    else:
        eligible = sorted(snapshot.effective_cpu_ids or [])
        processors = data.get("processors")
        arch = data.get("architecture")
        if (
            not isinstance(arch, str)
            or not arch.strip()
            or not isinstance(data.get("source"), str)
            or not data["source"].strip()
        ):
            reasons.append("architecture or observation source is missing")
        elif normalize_architecture(arch) not in {
            "x86_64",
            "i386",
            "i686",
            "aarch64",
            "armv7l",
            "armv8l",
        }:
            reasons.append("unsupported processor architecture")
        if not eligible or not isinstance(processors, list):
            reasons.append("eligible processor evidence is missing")
        elif (
            any(not isinstance(p, dict) or type(p.get("cpu_id")) is not int for p in processors)
            or sorted(p["cpu_id"] for p in processors) != eligible
        ):
            reasons.append("processor evidence does not cover exactly the eligible CPU boundary")
        else:
            identities = []
            for p in sorted(processors, key=lambda p: p["cpu_id"]):
                if (
                    any(
                        not isinstance(p.get(k), str) or not p[k].strip()
                        for k in ("vendor", "model")
                    )
                    or p.get("unavailable")
                    or not isinstance(p.get("attributes"), dict)
                    or any(
                        not isinstance(v, str) or not v for v in p.get("attributes", {}).values()
                    )
                ):
                    reasons.append(f"CPU {p['cpu_id']} has incomplete processor identity")
                else:
                    identities.append({k: p[k] for k in ("vendor", "model", "attributes")})
            if not reasons:
                # Order aligns with topology's normalized eligible-CPU order.
                # IDs, host names and sources are not part of the class hash.
                normalized = dict(architecture=normalize_architecture(arch), processors=identities)
        if data.get("complete") is not True or data.get("unavailable"):
            reasons.append("hardware observation is incomplete")
    if reasons:
        normalized = None
    return dict(
        schema_version=1,
        class_algorithm=CLASS_ALGORITHM,
        class_id=CLASS_ALGORITHM + ":" + _canonical_sha256(normalized) if normalized else None,
        description=normalized,
        observation=data,
        reasons=list(dict.fromkeys(reasons)),
        unavailable={
            key: "; ".join(reasons)
            for key, value in (
                ("class_id", normalized),
                ("description", normalized),
                ("observation", data),
            )
            if value is None
        },
        limitations=list(LIMITATIONS),
    )


def compare_hardware(saved: dict, current: ResourceSnapshot) -> dict:
    observed = hardware_context(current)
    before, now = saved.get("class_id"), observed["class_id"]
    status = (
        "unknown" if before is None or now is None else "matched" if before == now else "changed"
    )
    return dict(
        status=status,
        saved=saved,
        current=observed,
        explanation="Observed processor classes match; current performance remains unproven."
        if status == "matched"
        else "Hardware class changed or is unknown; explicitly revalidate for validated reuse, or acknowledge absent performance evidence with --allow-unvalidated.",
    )
