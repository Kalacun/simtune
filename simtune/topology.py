"""Conservative, read-only resource and topology discovery.

Parsing is kept separate from host reads so synthetic proc/sys/cgroup trees can
exercise Linux edge cases without depending on the machine running the tests.
The resulting record describes process-visible boundaries; it does not prove
NUMA policy enforcement or scheduler allocation semantics.
"""

from __future__ import annotations

import os
import platform as host_platform
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import TypeAlias

from . import util
from .hardware import HARDWARE_KEY, processor_identity
from .models import (
    CpuTopology,
    LimitState,
    MemoryLimit,
    NumaNode,
    Platform,
    ResourceSnapshot,
)

ReadText: TypeAlias = Callable[[Path], str]
ProbeRunner: TypeAlias = Callable[..., util.CommandResult]

_MAX_PSEUDO_FILE_CHARS = 1_048_576
_V1_UNLIMITED_MINIMUM = 1 << 60
_SLURM_HINTS = (
    "SLURM_JOB_ID",
    "SLURM_STEP_ID",
    "SLURM_JOB_NODELIST",
    "SLURM_JOB_NUM_NODES",
    "SLURM_NODEID",
    "SLURM_PROCID",
    "SLURM_LOCALID",
    "SLURM_NTASKS",
    "SLURM_NTASKS_PER_NODE",
    "SLURM_CPUS_PER_TASK",
    "SLURM_CPUS_ON_NODE",
    "SLURM_JOB_CPUS_PER_NODE",
    "SLURM_MEM_PER_NODE",
    "SLURM_MEM_PER_CPU",
    "SLURM_CPU_BIND",
    "SLURM_CPU_BIND_TYPE",
    "SLURM_CPU_BIND_LIST",
)


@dataclass(frozen=True)
class TopologyPaths:
    """Filesystem roots used by discovery, replaceable by synthetic trees."""

    proc_root: Path = Path("/proc")
    sys_root: Path = Path("/sys")
    cgroup_root: Path = Path("/sys/fs/cgroup")


@dataclass(frozen=True)
class CgroupMount:
    version: int
    root: str
    mount_point: str
    controllers: frozenset[str]


@dataclass(frozen=True)
class TopologyEnrichment:
    """Optional topology-only data; it can never supply resource masks."""

    cpus: list[CpuTopology] | None
    numa_nodes: list[NumaNode] | None
    source: str


@dataclass(frozen=True)
class HwlocAdapter:
    """Bounded optional hwloc probe, isolated behind an injectable runner."""

    executable: str
    runner: ProbeRunner = util.run_command

    @classmethod
    def from_system(cls) -> HwlocAdapter | None:
        path = util.which("lstopo-no-graphics") or util.which("lstopo")
        return cls(path) if path else None

    def collect(self) -> tuple[TopologyEnrichment | None, str | None]:
        result = self.runner([self.executable, "--of", "xml"], timeout=3)
        if result.returncode != 0:
            detail = util.compact_text(result.stderr or result.stdout).strip()
            return None, detail or f"{self.executable} failed with status {result.returncode}"
        if len(result.stdout) > _MAX_PSEUDO_FILE_CHARS:
            return None, "hwloc XML exceeds bounded probe output size"
        try:
            return parse_hwloc_xml(result.stdout, source=self.executable), None
        except (ET.ParseError, ValueError) as exc:
            return None, f"invalid hwloc XML: {exc}"


@dataclass(frozen=True)
class _ReadResult:
    text: str | None
    reason: str | None
    missing: bool = False


@dataclass
class _CgroupDiscovery:
    cpu_ids: set[int] | None
    cpu_source: str | None
    memory_node_ids: set[int] | None
    memory_node_source: str | None
    memory_limits: list[MemoryLimit]
    memory_limits_source: str | None
    conflicts: list[str]
    unavailable: list[str]


def parse_cpu_list(text: str) -> set[int]:
    """Parse Linux list syntax such as ``0-3,8,11`` without densifying IDs."""

    value = text.strip()
    if not value:
        return set()
    result: set[int] = set()
    for part in value.split(","):
        token = part.strip()
        if re.fullmatch(r"[0-9]+", token):
            result.add(int(token))
            continue
        match = re.fullmatch(r"([0-9]+)-([0-9]+)", token)
        if not match:
            raise ValueError(f"invalid CPU/node list token {token!r}")
        start, end = (int(item) for item in match.groups())
        if end < start:
            raise ValueError(f"descending CPU/node range {token!r}")
        result.update(range(start, end + 1))
    return result


def parse_proc_status(text: str) -> dict[str, str]:
    """Return the small set of process-boundary fields used by discovery."""

    wanted = {"Cpus_allowed_list", "Mems_allowed_list"}
    result: dict[str, str] = {}
    for line in text.splitlines():
        name, separator, value = line.partition(":")
        if separator and name in wanted:
            result[name] = value.strip()
    return result


def parse_proc_cgroup(text: str) -> dict[str, str]:
    """Map v1 controllers and the v2 unified hierarchy to membership paths."""

    result: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        fields = line.split(":", 2)
        if len(fields) != 3 or not fields[2].startswith("/"):
            raise ValueError(f"invalid /proc/self/cgroup line {number}")
        hierarchy, controllers, membership = fields
        if hierarchy == "0" and not controllers:
            result["__v2__"] = membership
        for controller in filter(None, controllers.split(",")):
            result[controller] = membership
    return result


def _unescape_mountinfo(value: str) -> str:
    replacements = {"\\040": " ", "\\011": "\t", "\\012": "\n", "\\134": "\\"}
    for escaped, literal in replacements.items():
        value = value.replace(escaped, literal)
    return value


def parse_mountinfo(text: str) -> list[CgroupMount]:
    """Parse cgroup mounts from Linux mountinfo, retaining namespace roots."""

    mounts: list[CgroupMount] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        before, separator, after = line.partition(" - ")
        left, right = before.split(), after.split()
        if not separator or len(left) < 6 or len(right) < 3:
            raise ValueError(f"invalid mountinfo line {number}")
        fs_type = right[0]
        if fs_type not in {"cgroup", "cgroup2"}:
            continue
        controllers: set[str] = set()
        if fs_type == "cgroup":
            controllers.update(right[2].split(","))
            controllers.update(left[5].split(","))
            controllers -= {"rw", "ro", "relatime", "nosuid", "nodev", "noexec"}
        mounts.append(
            CgroupMount(
                version=2 if fs_type == "cgroup2" else 1,
                root=_unescape_mountinfo(left[3]),
                mount_point=_unescape_mountinfo(left[4]),
                controllers=frozenset(controllers),
            )
        )
    return mounts


def parse_memory_limit(text: str, *, version: int) -> tuple[LimitState, int | None]:
    """Parse cgroup v1/v2 memory limits without comparing them to host RAM."""

    value = text.strip().lower()
    if value in {"max", "-1", "infinity"}:
        return LimitState.UNLIMITED, None
    if not re.fullmatch(r"[0-9]+", value):
        raise ValueError(f"invalid memory limit {text.strip()!r}")
    amount = int(value)
    if version == 1 and amount >= _V1_UNLIMITED_MINIMUM:
        return LimitState.UNLIMITED, None
    return LimitState.LIMITED, amount


def _parse_hwloc_bitmap(text: str) -> set[int]:
    """Parse hwloc's comma-separated 32-bit hexadecimal bitmap groups."""

    groups = text.strip().split(",")
    if not groups or any(re.fullmatch(r"0x[0-9a-fA-F]{1,8}", item) is None for item in groups):
        raise ValueError(f"invalid hwloc CPU bitmap {text!r}")
    bitmap = 0
    for group in groups:
        bitmap = (bitmap << 32) | int(group[2:], 16)
    return {cpu_id for cpu_id in range(bitmap.bit_length()) if bitmap & (1 << cpu_id)}


def parse_hwloc_xml(text: str, *, source: str = "hwloc") -> TopologyEnrichment:
    """Parse topology-only hwloc XML; no bitmap becomes an execution boundary."""

    root = ET.fromstring(text)
    raw_cpus: list[tuple[int, int | None, int | None]] = []
    raw_nodes: list[tuple[int, set[int], int | None]] = []

    def index(element: ET.Element) -> int | None:
        value = element.get("os_index")
        return int(value) if value is not None and value.isdigit() else None

    def walk(element: ET.Element, socket_id: int | None = None, core_id: int | None = None) -> None:
        object_type = element.get("type", "").lower()
        if object_type == "package":
            socket_id = index(element)
        elif object_type == "core":
            core_id = index(element)
        elif object_type == "pu":
            cpu_id = index(element)
            if cpu_id is not None:
                raw_cpus.append((cpu_id, socket_id, core_id))
        elif object_type == "numanode":
            node_id = index(element)
            bitmap = element.get("cpuset") or element.get("complete_cpuset")
            if node_id is not None and bitmap:
                cpu_ids = _parse_hwloc_bitmap(bitmap)
                memory_text = element.get("local_memory")
                memory = int(memory_text) if memory_text and memory_text.isdigit() else None
                raw_nodes.append((node_id, cpu_ids, memory))
        for child in element:
            if child.tag.rsplit("}", 1)[-1] == "object":
                walk(child, socket_id, core_id)

    object_roots = [item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == "object"]
    if object_roots:
        walk(object_roots[0])

    seen_cpu_ids = [item[0] for item in raw_cpus]
    if len(seen_cpu_ids) != len(set(seen_cpu_ids)):
        raise ValueError("hwloc XML contains duplicate PU IDs")
    seen_node_ids = [item[0] for item in raw_nodes]
    if len(seen_node_ids) != len(set(seen_node_ids)):
        raise ValueError("hwloc XML contains duplicate NUMA node IDs")

    sibling_groups: dict[tuple[int, int], list[int]] = {}
    for cpu_id, socket_id, core_id in raw_cpus:
        if socket_id is not None and core_id is not None:
            sibling_groups.setdefault((socket_id, core_id), []).append(cpu_id)
    node_by_cpu: dict[int, list[int]] = {}
    for node_id, cpu_ids, _memory in raw_nodes:
        for cpu_id in cpu_ids:
            node_by_cpu.setdefault(cpu_id, []).append(node_id)

    cpus = []
    for cpu_id, socket_id, core_id in sorted(raw_cpus):
        siblings = (
            sorted(sibling_groups[(socket_id, core_id)])
            if socket_id is not None and core_id is not None
            else None
        )
        mapped_nodes = node_by_cpu.get(cpu_id, [])
        numa_node = mapped_nodes[0] if len(mapped_nodes) == 1 else None
        unavailable = {}
        if socket_id is None:
            unavailable["socket_id"] = "hwloc did not report a package ID"
        if core_id is None:
            unavailable["core_id"] = "hwloc did not report a core ID"
        if siblings is None:
            unavailable["smt_siblings"] = "hwloc lacked package/core identity"
        if numa_node is None:
            unavailable["numa_node_id"] = (
                "hwloc mapped the CPU to multiple NUMA nodes"
                if len(mapped_nodes) > 1
                else "hwloc did not map the CPU to a NUMA node"
            )
        cpus.append(
            CpuTopology(
                cpu_id=cpu_id,
                socket_id=socket_id,
                core_id=core_id,
                smt_siblings=siblings,
                numa_node_id=numa_node,
                unavailable=unavailable,
            )
        )

    nodes = []
    for node_id, cpu_ids, memory in sorted(raw_nodes):
        unavailable = {"distances": "hwloc XML enrichment does not supply node distances"}
        if memory is None:
            unavailable["memory_bytes"] = "hwloc did not report local memory"
        nodes.append(
            NumaNode(
                node_id=node_id,
                cpu_ids=sorted(cpu_ids),
                memory_bytes=memory,
                distances=None,
                unavailable=unavailable,
            )
        )

    return TopologyEnrichment(
        cpus=cpus or None,
        numa_nodes=nodes or None,
        source=f"optional hwloc XML from {source}",
    )


def _default_read_text(path: Path) -> str:
    with path.open("r", encoding="utf-8", errors="strict") as handle:
        text = handle.read(_MAX_PSEUDO_FILE_CHARS + 1)
    if len(text) > _MAX_PSEUDO_FILE_CHARS:
        raise ValueError("pseudo-file exceeds bounded read size")
    return text


def _read(path: Path, reader: ReadText) -> _ReadResult:
    try:
        text = reader(path)
        if not isinstance(text, str):
            return _ReadResult(None, f"{path}: reader returned non-text data")
        if len(text) > _MAX_PSEUDO_FILE_CHARS:
            return _ReadResult(None, f"{path}: pseudo-file exceeds bounded read size")
        return _ReadResult(text, None)
    except FileNotFoundError:
        return _ReadResult(None, f"{path}: missing", missing=True)
    except (OSError, UnicodeError, ValueError) as exc:
        return _ReadResult(None, f"{path}: {type(exc).__name__}: {exc}")


def _read_id_set(path: Path, reader: ReadText) -> tuple[set[int] | None, str | None]:
    observed = _read(path, reader)
    if observed.text is None:
        return None, observed.reason
    try:
        return parse_cpu_list(observed.text), None
    except ValueError as exc:
        return None, f"{path}: {exc}"


def _read_nonnegative_int(path: Path, reader: ReadText) -> tuple[int | None, str | None]:
    observed = _read(path, reader)
    if observed.text is None:
        return None, observed.reason
    value = observed.text.strip()
    if not re.fullmatch(r"[0-9]+", value):
        return None, f"{path}: expected a nonnegative integer"
    return int(value), None


def _platform(value: str | Platform | None) -> Platform:
    if isinstance(value, Platform):
        return value
    name = value or util.detect_platform()
    try:
        return Platform(name)
    except ValueError:
        return Platform.OTHER


def _normalize_ids(values: Iterable[int]) -> set[int]:
    result: set[int] = set()
    for value in values:
        if type(value) is not int or value < 0:
            raise ValueError("affinity contains a non-integer or negative CPU ID")
        result.add(value)
    return result


def _join_reasons(reasons: Iterable[str], fallback: str) -> str:
    useful = list(dict.fromkeys(reason for reason in reasons if reason))
    return "; ".join(useful) if useful else fallback


def _discover_nodes(
    paths: TopologyPaths, reader: ReadText, conflicts: list[str]
) -> tuple[list[NumaNode] | None, str | None, str | None]:
    node_root = paths.sys_root / "devices/system/node"
    node_ids, node_error = _read_id_set(node_root / "online", reader)
    if node_ids is None:
        return None, None, node_error or "online NUMA nodes were not discovered"

    possible, possible_error = _read_id_set(node_root / "possible", reader)
    nodes: list[NumaNode] = []
    for node_id in sorted(node_ids):
        node_dir = node_root / f"node{node_id}"
        cpu_ids, cpu_error = _read_id_set(node_dir / "cpulist", reader)
        if cpu_ids is None:
            conflicts.append(cpu_error or f"NUMA node {node_id} CPU list is unavailable")
            return None, None, cpu_error or f"NUMA node {node_id} CPU list is unavailable"

        memory: int | None = None
        memory_reason: str | None = None
        meminfo = _read(node_dir / "meminfo", reader)
        if meminfo.text is None:
            memory_reason = meminfo.reason
        else:
            match = re.search(
                rf"^Node\s+{node_id}\s+MemTotal:\s*([0-9]+)\s+kB\s*$", meminfo.text, re.M
            )
            if match:
                memory = int(match.group(1)) * 1024
            else:
                memory_reason = f"{node_dir / 'meminfo'}: MemTotal was missing or malformed"

        distances: dict[str, int] | None = None
        distance_reason: str | None = None
        distance = _read(node_dir / "distance", reader)
        if distance.text is None:
            distance_reason = distance.reason
        elif possible is None:
            distance_reason = possible_error or "possible NUMA node IDs are unavailable"
        else:
            values = distance.text.split()
            if len(values) != len(possible) or any(not item.isdigit() for item in values):
                distance_reason = f"{node_dir / 'distance'}: values do not match possible node IDs"
            else:
                distances = {
                    str(target): int(value)
                    for target, value in zip(sorted(possible), values, strict=True)
                }

        unavailable = {}
        if memory is None:
            unavailable["memory_bytes"] = memory_reason or "NUMA node memory is unavailable"
        if distances is None:
            unavailable["distances"] = distance_reason or "NUMA distances are unavailable"
        nodes.append(
            NumaNode(
                node_id=node_id,
                cpu_ids=sorted(cpu_ids),
                memory_bytes=memory,
                distances=distances,
                unavailable=unavailable,
            )
        )
    return nodes, f"Linux sysfs {node_root}", None


def _cpu_node_from_directories(cpu_dir: Path) -> tuple[int | None, str | None]:
    try:
        matches = []
        for path in cpu_dir.glob("node[0-9]*"):
            match = re.fullmatch(r"node([0-9]+)", path.name)
            if match:
                matches.append(int(match.group(1)))
    except OSError as exc:
        return None, f"{cpu_dir}: {type(exc).__name__}: {exc}"
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, f"{cpu_dir}: CPU maps to multiple NUMA nodes {sorted(matches)}"
    return None, f"{cpu_dir}: no CPU-to-NUMA mapping"


def _discover_cpus(
    paths: TopologyPaths,
    reader: ReadText,
    online: set[int],
    nodes: list[NumaNode] | None,
    conflicts: list[str],
) -> list[CpuTopology]:
    cpu_root = paths.sys_root / "devices/system/cpu"
    node_by_cpu: dict[int, list[int]] = {}
    if nodes is not None:
        for node in nodes:
            for cpu_id in node.cpu_ids:
                node_by_cpu.setdefault(cpu_id, []).append(node.node_id)

    cpus: list[CpuTopology] = []
    for cpu_id in sorted(online):
        cpu_dir = cpu_root / f"cpu{cpu_id}"
        topology_dir = cpu_dir / "topology"
        socket_id, socket_reason = _read_nonnegative_int(
            topology_dir / "physical_package_id", reader
        )
        core_id, core_reason = _read_nonnegative_int(topology_dir / "core_id", reader)
        siblings, sibling_reason = _read_id_set(topology_dir / "thread_siblings_list", reader)
        if siblings is not None and cpu_id not in siblings:
            conflicts.append(f"CPU {cpu_id} is absent from its SMT sibling list {sorted(siblings)}")
            siblings = None
            sibling_reason = "SMT sibling list did not include this CPU"

        mapped_nodes = node_by_cpu.get(cpu_id, [])
        if len(mapped_nodes) == 1:
            numa_node, numa_reason = mapped_nodes[0], None
        elif len(mapped_nodes) > 1:
            numa_node, numa_reason = None, f"CPU appears in multiple nodes {mapped_nodes}"
            conflicts.append(f"CPU {cpu_id} appears in multiple NUMA nodes {mapped_nodes}")
        else:
            numa_node, numa_reason = _cpu_node_from_directories(cpu_dir)
            if numa_reason and nodes is not None:
                numa_reason = "CPU is absent from discovered NUMA node CPU lists"

        unavailable = {}
        for name, value, reason in (
            ("socket_id", socket_id, socket_reason),
            ("core_id", core_id, core_reason),
            ("smt_siblings", siblings, sibling_reason),
            ("numa_node_id", numa_node, numa_reason),
        ):
            if value is None:
                unavailable[name] = reason or f"{name} is unavailable"
        cpus.append(
            CpuTopology(
                cpu_id=cpu_id,
                socket_id=socket_id,
                core_id=core_id,
                smt_siblings=sorted(siblings) if siblings is not None else None,
                numa_node_id=numa_node,
                unavailable=unavailable,
            )
        )
    return cpus


def _merge_cpu_topology(
    current: list[CpuTopology] | None,
    enrichment: list[CpuTopology] | None,
    online: set[int] | None,
) -> tuple[list[CpuTopology] | None, bool]:
    if enrichment is None:
        return current, False
    enriched = {cpu.cpu_id: cpu for cpu in enrichment if online is None or cpu.cpu_id in online}
    if current is None:
        return (sorted(enriched.values(), key=lambda item: item.cpu_id) or None), bool(enriched)

    changed = False
    merged = []
    for cpu in current:
        extra = enriched.get(cpu.cpu_id)
        if extra is None:
            merged.append(cpu)
            continue
        values = {}
        unavailable = {}
        for name in ("socket_id", "core_id", "smt_siblings", "numa_node_id"):
            existing = getattr(cpu, name)
            replacement = getattr(extra, name)
            value = existing if existing is not None else replacement
            values[name] = value
            if existing is None and replacement is not None:
                changed = True
            if value is None:
                unavailable[name] = cpu.unavailable.get(
                    name, extra.unavailable.get(name, f"{name} is unavailable")
                )
        merged.append(
            CpuTopology(
                cpu_id=cpu.cpu_id,
                socket_id=values["socket_id"],
                core_id=values["core_id"],
                smt_siblings=values["smt_siblings"],
                numa_node_id=values["numa_node_id"],
                unavailable=unavailable,
            )
        )
    return merged, changed


def _merge_numa_topology(
    current: list[NumaNode] | None, enrichment: list[NumaNode] | None
) -> tuple[list[NumaNode] | None, bool]:
    if enrichment is None:
        return current, False
    if current is None:
        return enrichment, True
    enriched = {node.node_id: node for node in enrichment}
    changed = False
    merged = []
    for node in current:
        extra = enriched.get(node.node_id)
        if extra is None:
            merged.append(node)
            continue
        memory = node.memory_bytes
        distances = node.distances
        if memory is None and extra.memory_bytes is not None:
            memory = extra.memory_bytes
            changed = True
        if distances is None and extra.distances is not None:
            distances = extra.distances
            changed = True
        unavailable = {}
        if memory is None:
            unavailable["memory_bytes"] = node.unavailable.get(
                "memory_bytes", extra.unavailable.get("memory_bytes", "node memory is unavailable")
            )
        if distances is None:
            unavailable["distances"] = node.unavailable.get(
                "distances", extra.unavailable.get("distances", "node distances are unavailable")
            )
        merged.append(
            NumaNode(
                node_id=node.node_id,
                cpu_ids=node.cpu_ids,
                memory_bytes=memory,
                distances=distances,
                unavailable=unavailable,
            )
        )
    return merged, changed


def _local_mount_path(mount_point: str, paths: TopologyPaths) -> Path:
    logical = PurePosixPath(mount_point)
    standard = PurePosixPath("/sys/fs/cgroup")
    try:
        relative = logical.relative_to(standard)
    except ValueError:
        try:
            relative = logical.relative_to(PurePosixPath("/sys"))
        except ValueError:
            return Path(mount_point)
        return paths.sys_root.joinpath(*relative.parts)
    return paths.cgroup_root.joinpath(*relative.parts)


def _membership_directory(
    mount: CgroupMount, membership: str, paths: TopologyPaths
) -> tuple[Path | None, str | None]:
    if any(part == ".." for part in PurePosixPath(membership).parts):
        return None, f"unsafe cgroup membership path {membership!r}"
    root = PurePosixPath(mount.root)
    member = PurePosixPath(membership)
    try:
        if membership == "/":
            relative = PurePosixPath(".")
        else:
            relative = member.relative_to(root)
    except ValueError:
        return None, f"cgroup membership {membership!r} is outside mount root {mount.root!r}"
    base = _local_mount_path(mount.mount_point, paths)
    return base.joinpath(*relative.parts), None


def _ancestors(leaf: Path, mount_root: Path) -> list[Path]:
    try:
        leaf.relative_to(mount_root)
    except ValueError:
        return []
    result = []
    current = leaf
    while True:
        result.append(current)
        if current == mount_root:
            return result
        current = current.parent


def _select_mount(
    mounts: list[CgroupMount], controller: str, membership: dict[str, str]
) -> tuple[CgroupMount | None, str | None, str | None]:
    candidates: list[tuple[CgroupMount, str]] = []
    for mount in mounts:
        if mount.version == 2 and "__v2__" in membership:
            candidates.append((mount, membership["__v2__"]))
        elif mount.version == 1 and controller in mount.controllers and controller in membership:
            candidates.append((mount, membership[controller]))
    if not candidates:
        return None, None, f"no cgroup mount and membership found for {controller}"
    candidates.sort(key=lambda item: len(PurePosixPath(item[0].root).parts), reverse=True)
    mount, member = candidates[0]
    return mount, member, None


def _read_cpuset_chain(
    mount: CgroupMount,
    leaf: Path,
    paths: TopologyPaths,
    kind: str,
    reader: ReadText,
) -> tuple[set[int] | None, str | None, list[str]]:
    mount_root = _local_mount_path(mount.mount_point, paths)
    effective_names = (
        (f"cpuset.{kind}.effective",)
        if mount.version == 2
        else (f"cpuset.effective_{kind}", f"cpuset.{kind}.effective")
    )
    raw_name = f"cpuset.{kind}"
    observations: list[set[int]] = []
    sources: list[str] = []
    errors: list[str] = []
    malformed = False

    for directory in _ancestors(leaf, mount_root):
        effective_found = False
        for name in effective_names:
            path = directory / name
            observed = _read(path, reader)
            if observed.text is None:
                if not observed.missing:
                    errors.append(observed.reason or f"{path}: unreadable")
                continue
            effective_found = True
            try:
                value = parse_cpu_list(observed.text)
            except ValueError as exc:
                errors.append(f"{path}: {exc}")
                return None, None, errors
            if malformed:
                return None, None, errors
            return value, str(path), errors
        if effective_found:
            continue

        path = directory / raw_name
        observed = _read(path, reader)
        if observed.text is None:
            if not observed.missing:
                errors.append(observed.reason or f"{path}: unreadable")
                malformed = True
            continue
        if not observed.text.strip():
            continue
        try:
            observations.append(parse_cpu_list(observed.text))
            sources.append(str(path))
        except ValueError as exc:
            errors.append(f"{path}: {exc}")
            malformed = True

    if malformed:
        return None, None, errors
    if observations:
        return set.intersection(*observations), " intersect ".join(sources), errors
    errors.append(f"no effective or inherited {raw_name} value was readable")
    return None, None, errors


def _memory_limits(
    mount: CgroupMount,
    leaf: Path,
    paths: TopologyPaths,
    reader: ReadText,
) -> tuple[list[MemoryLimit], list[str]]:
    mount_root = _local_mount_path(mount.mount_point, paths)
    filename = "memory.max" if mount.version == 2 else "memory.limit_in_bytes"
    limits: list[MemoryLimit] = []
    errors: list[str] = []
    any_file = False
    for directory in _ancestors(leaf, mount_root):
        path = directory / filename
        observed = _read(path, reader)
        if observed.text is None:
            if not observed.missing:
                reason = observed.reason or f"{path}: unreadable"
                errors.append(reason)
                limits.append(
                    MemoryLimit(
                        source=str(path),
                        scope=f"cgroup v{mount.version} {directory}",
                        state=LimitState.UNKNOWN,
                        bytes=None,
                        unavailable={"bytes": reason},
                    )
                )
            continue
        any_file = True
        try:
            state, amount = parse_memory_limit(observed.text, version=mount.version)
            reason = "controller reports no finite limit" if amount is None else None
        except ValueError as exc:
            state, amount = LimitState.UNKNOWN, None
            reason = f"{path}: {exc}"
            errors.append(reason)
        limits.append(
            MemoryLimit(
                source=str(path),
                scope=f"cgroup v{mount.version} {directory}",
                state=state,
                bytes=amount,
                unavailable={"bytes": reason} if reason else {},
            )
        )
    if not any_file and not limits:
        reason = f"{leaf / filename}: memory limit file is missing"
        errors.append(reason)
        limits.append(
            MemoryLimit(
                source=str(leaf / filename),
                scope=f"cgroup v{mount.version} {leaf}",
                state=LimitState.UNKNOWN,
                bytes=None,
                unavailable={"bytes": reason},
            )
        )
    return limits, errors


def _discover_cgroups(paths: TopologyPaths, reader: ReadText) -> _CgroupDiscovery:
    unavailable: list[str] = []
    conflicts: list[str] = []
    mount_text = _read(paths.proc_root / "self/mountinfo", reader)
    membership_text = _read(paths.proc_root / "self/cgroup", reader)
    if mount_text.text is None or membership_text.text is None:
        unavailable.extend(
            item for item in (mount_text.reason, membership_text.reason) if item is not None
        )
        return _CgroupDiscovery(None, None, None, None, [], None, conflicts, unavailable)
    try:
        mounts = parse_mountinfo(mount_text.text)
        memberships = parse_proc_cgroup(membership_text.text)
    except ValueError as exc:
        unavailable.append(str(exc))
        conflicts.append(f"cgroup metadata is malformed: {exc}")
        return _CgroupDiscovery(None, None, None, None, [], None, conflicts, unavailable)

    cpus = mems = None
    cpu_source = mem_source = None
    cpuset_mount, cpuset_member, cpuset_error = _select_mount(mounts, "cpuset", memberships)
    if cpuset_mount is None or cpuset_member is None:
        unavailable.append(cpuset_error or "cgroup cpuset membership is unavailable")
    else:
        leaf, leaf_error = _membership_directory(cpuset_mount, cpuset_member, paths)
        if leaf is None:
            unavailable.append(leaf_error or "cgroup cpuset path is unavailable")
            conflicts.append(leaf_error or "cgroup cpuset path is unavailable")
        else:
            cpus, cpu_source, cpu_errors = _read_cpuset_chain(
                cpuset_mount, leaf, paths, "cpus", reader
            )
            mems, mem_source, mem_errors = _read_cpuset_chain(
                cpuset_mount, leaf, paths, "mems", reader
            )
            unavailable.extend(cpu_errors + mem_errors)
            conflicts.extend(
                f"cgroup cpuset observation is incomplete: {error}"
                for error in cpu_errors + mem_errors
                if "no effective or inherited" not in error
            )

    memory_limits: list[MemoryLimit] = []
    memory_source = None
    memory_mount, memory_member, memory_error = _select_mount(mounts, "memory", memberships)
    if memory_mount is None or memory_member is None:
        unavailable.append(memory_error or "cgroup memory membership is unavailable")
    else:
        leaf, leaf_error = _membership_directory(memory_mount, memory_member, paths)
        if leaf is None:
            unavailable.append(leaf_error or "cgroup memory path is unavailable")
            conflicts.append(leaf_error or "cgroup memory path is unavailable")
        else:
            memory_limits, memory_errors = _memory_limits(memory_mount, leaf, paths, reader)
            memory_source = f"cgroup v{memory_mount.version} hierarchy and ancestors"
            unavailable.extend(memory_errors)
            conflicts.extend(
                f"cgroup memory observation is incomplete: {error}" for error in memory_errors
            )

    final_membership = _read(paths.proc_root / "self/cgroup", reader)
    if final_membership.text != membership_text.text:
        reason = (
            "cgroup membership changed during discovery"
            if final_membership.text is not None
            else f"cgroup membership could not be rechecked: {final_membership.reason}"
        )
        conflicts.append(reason)
        unavailable.append(reason)
        cpus = None
        cpu_source = None
        mems = None
        mem_source = None

    return _CgroupDiscovery(
        cpus,
        cpu_source,
        mems,
        mem_source,
        memory_limits,
        memory_source,
        conflicts,
        unavailable,
    )


def _scheduler_hints(environ: Mapping[str, str], conflicts: list[str]) -> dict[str, str]:
    hints = {}
    for name in _SLURM_HINTS:
        if name not in environ:
            continue
        value = environ[name]
        if not isinstance(value, str) or "\0" in value:
            conflicts.append(f"scheduler hint {name} was not safe text and was ignored")
            continue
        hints[name] = value
    return hints


def _unknown_snapshot(
    snapshot_id: str, platform: Platform, is_wsl: bool, reason: str, environ: Mapping[str, str]
) -> ResourceSnapshot:
    conflicts = [reason]
    hints = _scheduler_hints(environ, conflicts)
    fields = (
        "online_cpu_ids",
        "cpus",
        "numa_nodes",
        "inherited_cpu_ids",
        "permitted_cpu_ids",
        "effective_cpu_ids",
        "allowed_memory_node_ids",
    )
    return ResourceSnapshot(
        id=snapshot_id,
        platform=platform,
        is_wsl=is_wsl,
        online_cpu_ids=None,
        cpus=None,
        numa_nodes=None,
        inherited_cpu_ids=None,
        permitted_cpu_ids=None,
        effective_cpu_ids=None,
        allowed_memory_node_ids=None,
        memory_limits=[],
        sources={"scheduler_hints": "allowlisted environment metadata"} if hints else {},
        scheduler_hints=hints,
        conflicts=conflicts,
        unavailable={name: reason for name in fields},
    )


def discover_resource_snapshot(
    *,
    snapshot_id: str = "current-process",
    platform_name: str | Platform | None = None,
    is_wsl: bool | None = None,
    paths: TopologyPaths | None = None,
    affinity_reader: Callable[[], Iterable[int]] | None = None,
    environ: Mapping[str, str] | None = None,
    read_text: ReadText | None = None,
    hwloc_adapter: HwlocAdapter | None = None,
    architecture: str | None = None,
) -> ResourceSnapshot:
    """Discover a conservative process-visible snapshot.

    Scheduler variables are copied only as untrusted hints. They never become
    CPU or node masks. Every filesystem access is read-only and bounded.
    """

    platform = _platform(platform_name)
    wsl = (util.is_wsl() if platform == Platform.LINUX else False) if is_wsl is None else is_wsl
    environment = os.environ if environ is None else environ
    roots = paths or TopologyPaths()
    reader = read_text or _default_read_text
    if platform == Platform.LINUX and paths is None and hwloc_adapter is None:
        hwloc_adapter = HwlocAdapter.from_system()
    try:
        snapshot = _discover_resource_snapshot(
            snapshot_id=snapshot_id,
            platform=platform,
            is_wsl=wsl,
            paths=roots,
            affinity_reader=affinity_reader,
            environ=environment,
            reader=reader,
            hwloc_adapter=hwloc_adapter,
        )
    except Exception as exc:  # OS discovery must not make doctor unusable.
        reason = util.compact_text(
            f"resource discovery failed safely: {type(exc).__name__}: {exc}"
        ).replace("\0", "?")
        snapshot = _unknown_snapshot(snapshot_id, platform, wsl, reason, environment)
    identity_source = str(roots.proc_root / "cpuinfo")
    try:
        observation = (
            _read(roots.proc_root / "cpuinfo", reader) if platform == Platform.LINUX else None
        )
    except Exception as exc:
        observation = _ReadResult(None, util.compact_text(f"processor discovery failed: {exc}"))
    identity = processor_identity(
        observation.text if observation else None,
        snapshot.effective_cpu_ids,
        architecture if architecture is not None else host_platform.machine(),
        identity_source if observation else "unsupported platform; no processor provider",
        observation.reason if observation else "no supported read-only processor provider",
    )
    return replace(snapshot, extensions={**snapshot.extensions, HARDWARE_KEY: identity})


def _discover_resource_snapshot(
    *,
    snapshot_id: str,
    platform: Platform,
    is_wsl: bool,
    paths: TopologyPaths,
    affinity_reader: Callable[[], Iterable[int]] | None,
    environ: Mapping[str, str],
    reader: ReadText,
    hwloc_adapter: HwlocAdapter | None,
) -> ResourceSnapshot:
    conflicts: list[str] = []
    sources: dict[str, str] = {}
    unavailable: dict[str, str] = {}
    hints = _scheduler_hints(environ, conflicts)
    if hints:
        sources["scheduler_hints"] = (
            "allowlisted environment metadata; values are untrusted hints, never CPU masks"
        )

    nullable_fields = (
        "online_cpu_ids",
        "cpus",
        "numa_nodes",
        "inherited_cpu_ids",
        "permitted_cpu_ids",
        "effective_cpu_ids",
        "allowed_memory_node_ids",
    )
    if platform != Platform.LINUX:
        reason = f"{platform.value} has no trusted Linux CPU/NUMA resource discovery"
        return ResourceSnapshot(
            id=snapshot_id,
            platform=platform,
            is_wsl=False,
            online_cpu_ids=None,
            cpus=None,
            numa_nodes=None,
            inherited_cpu_ids=None,
            permitted_cpu_ids=None,
            effective_cpu_ids=None,
            allowed_memory_node_ids=None,
            memory_limits=[],
            sources=sources,
            scheduler_hints=hints,
            conflicts=conflicts,
            unavailable={name: reason for name in nullable_fields},
        )

    cpu_root = paths.sys_root / "devices/system/cpu"
    online, online_error = _read_id_set(cpu_root / "online", reader)
    if online is not None:
        sources["online_cpu_ids"] = f"Linux sysfs {cpu_root / 'online'}"
    else:
        unavailable["online_cpu_ids"] = online_error or "online CPUs were not discovered"

    nodes, node_source, node_error = _discover_nodes(paths, reader, conflicts)
    if nodes is not None:
        sources["numa_nodes"] = node_source or "Linux sysfs NUMA nodes"
    else:
        unavailable["numa_nodes"] = node_error or "NUMA nodes were not discovered"

    cpus = None
    if online is not None:
        cpus = _discover_cpus(paths, reader, online, nodes, conflicts)
        sources["cpus"] = f"Linux sysfs topology for IDs from {cpu_root / 'online'}"
    else:
        unavailable["cpus"] = "CPU topology requires a trusted online CPU list"

    if hwloc_adapter is not None and (
        cpus is None
        or nodes is None
        or any(cpu.unavailable for cpu in cpus)
        or any(node.unavailable for node in nodes)
    ):
        enrichment, enrichment_error = hwloc_adapter.collect()
        if enrichment is not None:
            nodes, nodes_changed = _merge_numa_topology(nodes, enrichment.numa_nodes)
            cpus, cpus_changed = _merge_cpu_topology(cpus, enrichment.cpus, online)
            if nodes_changed:
                sources["numa_nodes"] = " + ".join(
                    filter(None, (sources.get("numa_nodes"), enrichment.source))
                )
                unavailable.pop("numa_nodes", None)
            if cpus_changed:
                sources["cpus"] = " + ".join(filter(None, (sources.get("cpus"), enrichment.source)))
                unavailable.pop("cpus", None)
        elif enrichment_error:
            if cpus is None:
                unavailable["cpus"] = (
                    f"{unavailable.get('cpus', 'CPU topology is unavailable')}; "
                    f"optional hwloc: {enrichment_error}"
                )
            if nodes is None:
                unavailable["numa_nodes"] = (
                    f"{unavailable.get('numa_nodes', 'NUMA topology is unavailable')}; "
                    f"optional hwloc: {enrichment_error}"
                )

    status_result = _read(paths.proc_root / "self/status", reader)
    status = parse_proc_status(status_result.text) if status_result.text is not None else {}
    status_cpu = status_mems = None
    status_cpu_error = status_mems_error = None
    if "Cpus_allowed_list" in status:
        try:
            status_cpu = parse_cpu_list(status["Cpus_allowed_list"])
        except ValueError as exc:
            status_cpu_error = f"/proc/self/status Cpus_allowed_list: {exc}"
    else:
        status_cpu_error = status_result.reason or "/proc/self/status lacks Cpus_allowed_list"
    if "Mems_allowed_list" in status:
        try:
            status_mems = parse_cpu_list(status["Mems_allowed_list"])
        except ValueError as exc:
            status_mems_error = f"/proc/self/status Mems_allowed_list: {exc}"
    else:
        status_mems_error = status_result.reason or "/proc/self/status lacks Mems_allowed_list"

    affinity = None
    affinity_error = None
    if affinity_reader is None and hasattr(os, "sched_getaffinity"):

        def affinity_reader() -> Iterable[int]:
            return os.sched_getaffinity(0)

    if affinity_reader is not None:
        try:
            affinity = _normalize_ids(affinity_reader())
        except (OSError, TypeError, ValueError) as exc:
            affinity_error = f"sched_getaffinity: {type(exc).__name__}: {exc}"
    else:
        affinity_error = "sched_getaffinity is unavailable"

    inherited_sources = []
    inherited_observations = []
    if affinity is not None:
        inherited_observations.append(affinity)
        inherited_sources.append("sched_getaffinity(0)")
    if status_cpu is not None:
        inherited_observations.append(status_cpu)
        inherited_sources.append("/proc/self/status Cpus_allowed_list")
    inherited = set.intersection(*inherited_observations) if inherited_observations else None
    if inherited is not None:
        sources["inherited_cpu_ids"] = " intersect ".join(inherited_sources)
        if len(inherited_observations) > 1 and affinity != status_cpu:
            conflicts.append(
                "sched_getaffinity and /proc/self/status report different inherited CPU sets; "
                "the conservative intersection was retained"
            )
    else:
        unavailable["inherited_cpu_ids"] = _join_reasons(
            (affinity_error or "", status_cpu_error or ""),
            "inherited CPU affinity was not discovered",
        )

    cgroups = _discover_cgroups(paths, reader)
    conflicts.extend(cgroups.conflicts)
    permitted = cgroups.cpu_ids
    if permitted is not None:
        sources["permitted_cpu_ids"] = cgroups.cpu_source or "cgroup cpuset"
    else:
        unavailable["permitted_cpu_ids"] = _join_reasons(
            cgroups.unavailable, "cgroup CPU permission was not discovered"
        )

    memory_observations = []
    memory_sources = []
    if status_mems is not None:
        memory_observations.append(status_mems)
        memory_sources.append("/proc/self/status Mems_allowed_list")
    if cgroups.memory_node_ids is not None:
        memory_observations.append(cgroups.memory_node_ids)
        memory_sources.append(cgroups.memory_node_source or "cgroup cpuset memory nodes")
    allowed_mems = set.intersection(*memory_observations) if memory_observations else None
    if allowed_mems is not None:
        sources["allowed_memory_node_ids"] = " intersect ".join(memory_sources)
        if len(memory_observations) > 1 and status_mems != cgroups.memory_node_ids:
            conflicts.append(
                "/proc/self/status and cgroup cpuset report different allowed memory nodes; "
                "the conservative intersection was retained"
            )
    else:
        unavailable["allowed_memory_node_ids"] = _join_reasons(
            (status_mems_error or "", *cgroups.unavailable),
            "allowed memory nodes were not discovered",
        )

    # Retain partial discovery for diagnostics. Strict launch authorization must
    # separately require inherited task affinity; an online/cgroup intersection
    # alone cannot establish a narrower voluntary task mask.
    boundaries = [item for item in (online, inherited, permitted) if item is not None]
    effective = set.intersection(*boundaries) if boundaries else None
    if effective is not None:
        names = [
            name
            for name, value in (
                ("online_cpu_ids", online),
                ("inherited_cpu_ids", inherited),
                ("permitted_cpu_ids", permitted),
            )
            if value is not None
        ]
        sources["effective_cpu_ids"] = f"intersection of known boundaries: {', '.join(names)}"
        if not effective:
            conflicts.append("known CPU boundaries have an empty intersection")
    else:
        unavailable["effective_cpu_ids"] = "no CPU boundary was discovered"

    if online is not None and inherited is not None and not inherited <= online:
        conflicts.append(
            f"inherited CPU affinity includes CPUs not reported online: {sorted(inherited - online)}"
        )
    if online is not None and permitted is not None and not permitted <= online:
        conflicts.append(
            f"cgroup CPU permission includes CPUs not reported online: {sorted(permitted - online)}"
        )
    if nodes is not None and allowed_mems is not None:
        discovered_nodes = {node.node_id for node in nodes}
        if not allowed_mems <= discovered_nodes:
            conflicts.append(
                "allowed memory nodes are absent from online NUMA topology: "
                f"{sorted(allowed_mems - discovered_nodes)}"
            )

    if cgroups.memory_limits:
        sources["memory_limits"] = cgroups.memory_limits_source or "cgroup memory hierarchy"

    if is_wsl:
        conflicts.append(
            "WSL topology is descriptive only; automatic NUMA memory placement remains unsupported"
        )

    values = {
        "online_cpu_ids": online,
        "cpus": cpus,
        "numa_nodes": nodes,
        "inherited_cpu_ids": inherited,
        "permitted_cpu_ids": permitted,
        "effective_cpu_ids": effective,
        "allowed_memory_node_ids": allowed_mems,
    }
    for name, value in values.items():
        if value is None and name not in unavailable:
            unavailable[name] = f"{name} was not discovered"

    return ResourceSnapshot(
        id=snapshot_id,
        platform=platform,
        is_wsl=is_wsl,
        online_cpu_ids=sorted(online) if online is not None else None,
        cpus=cpus,
        numa_nodes=nodes,
        inherited_cpu_ids=sorted(inherited) if inherited is not None else None,
        permitted_cpu_ids=sorted(permitted) if permitted is not None else None,
        effective_cpu_ids=sorted(effective) if effective is not None else None,
        allowed_memory_node_ids=sorted(allowed_mems) if allowed_mems is not None else None,
        memory_limits=cgroups.memory_limits,
        sources=sources,
        scheduler_hints=hints,
        conflicts=list(dict.fromkeys(conflicts)),
        unavailable=unavailable,
    )
