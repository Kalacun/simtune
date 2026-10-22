"""Synthetic topology tests; none are native Linux, NUMA, or Slurm evidence."""

from __future__ import annotations

from pathlib import Path

import pytest

from simtune import util
from simtune.hardware import HARDWARE_KEY, hardware_context, processor_identity
from simtune.models import LimitState, ResourceSnapshot
from simtune.topology import (
    HwlocAdapter,
    TopologyPaths,
    discover_resource_snapshot,
    parse_cpu_list,
    parse_memory_limit,
    parse_mountinfo,
    parse_proc_cgroup,
)


def _put(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_hardware_observation_covers_eligible_ids_only_and_roundtrips(tmp_path):
    paths = _synthetic_v2(tmp_path)
    text = "\n\n".join(
        f"processor : {cpu}\nvendor_id : Vendor\nmodel name : Model {cpu}\n"
        for cpu in (2, 6, 10, 11)
    )
    _put(tmp_path, "proc/cpuinfo", text)
    snapshot = discover_resource_snapshot(
        platform_name="linux",
        paths=paths,
        affinity_reader=lambda: [2, 6, 10],
        environ={},
        architecture="amd64",
    )
    data = snapshot.extensions[HARDWARE_KEY]
    assert [p["cpu_id"] for p in data["processors"]] == snapshot.effective_cpu_ids == [2, 6]
    assert data["source"] == str(paths.proc_root / "cpuinfo")
    assert data["complete"] and data["architecture"] == "x86_64"
    assert hardware_context(snapshot)["class_id"]
    assert ResourceSnapshot.from_json(snapshot.to_json()) == snapshot


@pytest.mark.parametrize(
    "problem", ["missing", "duplicate", "model", "vendor", "architecture", "coverage", "version"]
)
def test_hardware_missing_or_ambiguous_is_never_a_class(tmp_path, problem):
    from dataclasses import replace

    paths = _synthetic_v2(tmp_path)
    text = "processor : 2\nvendor_id : Vendor\nmodel name : Model\n\nprocessor : 6\nvendor_id : Vendor\nmodel name : Model\n"
    if problem == "missing":
        text = ""
    elif problem == "duplicate":
        text += "\nprocessor : 2\nvendor_id : Vendor\nmodel name : Model\n"
    elif problem in {"model", "vendor"}:
        text = (
            text.replace("model name : Model", "")
            if problem == "model"
            else text.replace("vendor_id : Vendor", "")
        )
    _put(tmp_path, "proc/cpuinfo", text)
    snapshot = discover_resource_snapshot(
        platform_name="linux",
        paths=paths,
        affinity_reader=lambda: [2, 6],
        environ={},
        architecture="unknown-architecture" if problem == "architecture" else "x86_64",
    )
    if problem in {"coverage", "version"}:
        data = dict(snapshot.extensions[HARDWARE_KEY])
        if problem == "coverage":
            data["processors"] = data["processors"][:1]
        else:
            data["schema_version"] = True
        snapshot = replace(snapshot, extensions={HARDWARE_KEY: data})
    context = hardware_context(snapshot)
    assert context["class_id"] is None and context["reasons"] and context["unavailable"]


def test_heterogeneous_arm_per_cpu_identity_does_not_use_global_board_name():
    text = "Hardware : Board\nProcessor : generic AArch64\n\n" + "\n\n".join(
        f"processor : {i}\nCPU implementer : 0x41\nCPU architecture : 8\nCPU part : {part}\nCPU variant : 0x0\nCPU revision : 1\n"
        for i, part in ((1, "0xd03"), (7, "0xd08"))
    )
    identity = processor_identity(text, [1, 7], "arm64", "synthetic Linux cpuinfo")
    assert identity["architecture"] == "aarch64" and identity["complete"]
    assert identity["processors"][0]["model"] != identity["processors"][1]["model"]
    incomplete = processor_identity(
        text.replace("CPU part : 0xd08", ""), [1, 7], "aarch64", "synthetic"
    )
    assert not incomplete["complete"]
    assert incomplete["processors"][1]["model"] is None


def test_cpuinfo_reads_are_bounded(tmp_path):
    paths = _synthetic_v2(tmp_path)
    _put(tmp_path, "proc/cpuinfo", "x" * (1_048_576 + 1))
    snapshot = discover_resource_snapshot(
        platform_name="linux",
        paths=paths,
        affinity_reader=lambda: [2, 6],
        environ={},
        architecture="x86_64",
    )
    identity = snapshot.extensions[HARDWARE_KEY]
    assert not identity["complete"]
    assert "bounded" in identity["processors"][0]["unavailable"]["model"]


def _paths(root: Path) -> TopologyPaths:
    return TopologyPaths(
        proc_root=root / "proc",
        sys_root=root / "sys",
        cgroup_root=root / "sys/fs/cgroup",
    )


def _cpu(root: Path, cpu_id: int, *, socket: int, core: int, siblings: str) -> None:
    base = f"sys/devices/system/cpu/cpu{cpu_id}/topology"
    _put(root, f"{base}/physical_package_id", f"{socket}\n")
    _put(root, f"{base}/core_id", f"{core}\n")
    _put(root, f"{base}/thread_siblings_list", f"{siblings}\n")


def _node(root: Path, node_id: int, cpus: str, memory_kib: int, distance: str) -> None:
    base = f"sys/devices/system/node/node{node_id}"
    _put(root, f"{base}/cpulist", f"{cpus}\n")
    _put(root, f"{base}/meminfo", f"Node {node_id} MemTotal: {memory_kib} kB\n")
    _put(root, f"{base}/distance", f"{distance}\n")


def _synthetic_v2(root: Path) -> TopologyPaths:
    _put(root, "sys/devices/system/cpu/online", "2,6,10-11\n")
    _cpu(root, 2, socket=0, core=0, siblings="2,10")
    _cpu(root, 10, socket=0, core=0, siblings="2,10")
    _cpu(root, 6, socket=1, core=0, siblings="6,11")
    _cpu(root, 11, socket=1, core=0, siblings="6,11")
    # A directory for an offline ID must never make that CPU usable.
    _cpu(root, 4, socket=9, core=9, siblings="4")

    _put(root, "sys/devices/system/node/online", "0,2\n")
    _put(root, "sys/devices/system/node/possible", "0,2\n")
    _node(root, 0, "2,10", 1024, "10 21")
    _node(root, 2, "6,11", 2048, "21 10")

    _put(
        root,
        "proc/self/status",
        "Name:\tsynthetic\nCpus_allowed_list:\t2,6,10\nMems_allowed_list:\t0,2\n",
    )
    _put(root, "proc/self/cgroup", "0::/tenant/job/step\n")
    _put(
        root,
        "proc/self/mountinfo",
        "36 29 0:32 /tenant /sys/fs/cgroup rw,nosuid,nodev,noexec,relatime - cgroup2 cgroup rw\n",
    )
    _put(root, "sys/fs/cgroup/job/step/cpuset.cpus.effective", "2,6,11\n")
    _put(root, "sys/fs/cgroup/job/step/cpuset.mems.effective", "2\n")
    _put(root, "sys/fs/cgroup/job/step/memory.max", "max\n")
    _put(root, "sys/fs/cgroup/job/memory.max", "1048576\n")
    _put(root, "sys/fs/cgroup/memory.max", "2097152\n")
    return _paths(root)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_v2_sparse_topology_boundaries_ancestors_and_scheduler_hints(tmp_path):
    paths = _synthetic_v2(tmp_path)
    before = _tree_bytes(tmp_path)
    snapshot = discover_resource_snapshot(
        snapshot_id="v2-synthetic",
        platform_name="linux",
        is_wsl=False,
        paths=paths,
        affinity_reader=lambda: {2, 6, 10},
        environ={
            "SLURM_CPUS_PER_TASK": "99",
            "SLURM_CPU_BIND_LIST": "0-98",
            "UNRELATED_SECRET": "not captured",
        },
    )

    assert snapshot.online_cpu_ids == [2, 6, 10, 11]
    assert [cpu.cpu_id for cpu in snapshot.cpus or []] == [2, 6, 10, 11]
    assert 4 not in (snapshot.effective_cpu_ids or [])
    assert snapshot.inherited_cpu_ids == [2, 6, 10]
    assert snapshot.permitted_cpu_ids == [2, 6, 11]
    assert snapshot.effective_cpu_ids == [2, 6]
    assert snapshot.allowed_memory_node_ids == [2]
    assert snapshot.scheduler_hints == {
        "SLURM_CPUS_PER_TASK": "99",
        "SLURM_CPU_BIND_LIST": "0-98",
    }
    assert 98 not in snapshot.effective_cpu_ids

    cpus = {cpu.cpu_id: cpu for cpu in snapshot.cpus or []}
    assert (cpus[2].socket_id, cpus[2].core_id) == (0, 0)
    assert (cpus[6].socket_id, cpus[6].core_id) == (1, 0)
    assert cpus[2].smt_siblings == [2, 10]
    assert cpus[6].numa_node_id == 2
    nodes = {node.node_id: node for node in snapshot.numa_nodes or []}
    assert nodes[0].memory_bytes == 1024 * 1024
    assert nodes[2].distances == {"0": 21, "2": 10}

    assert [(limit.state, limit.bytes) for limit in snapshot.memory_limits] == [
        (LimitState.UNLIMITED, None),
        (LimitState.LIMITED, 1048576),
        (LimitState.LIMITED, 2097152),
    ]
    assert any("allowed memory nodes" in item for item in snapshot.conflicts)
    assert ResourceSnapshot.from_json(snapshot.to_json()) == snapshot
    assert _tree_bytes(tmp_path) == before


def test_v1_empty_cpuset_files_inherit_and_large_limit_is_unlimited(tmp_path):
    _put(tmp_path, "sys/devices/system/cpu/online", "8,12\n")
    _cpu(tmp_path, 8, socket=0, core=3, siblings="8")
    _cpu(tmp_path, 12, socket=1, core=3, siblings="12")
    _put(tmp_path, "sys/devices/system/node/online", "1\n")
    _put(tmp_path, "sys/devices/system/node/possible", "1\n")
    _node(tmp_path, 1, "8,12", 512, "10")
    _put(
        tmp_path,
        "proc/self/status",
        "Cpus_allowed_list:\t8,12\nMems_allowed_list:\t1\n",
    )
    _put(
        tmp_path,
        "proc/self/cgroup",
        "5:cpuset:/batch/job\n4:memory:/batch/job\n",
    )
    _put(
        tmp_path,
        "proc/self/mountinfo",
        "30 20 0:30 /batch /sys/fs/cgroup/cpuset rw - cgroup cgroup rw,cpuset\n"
        "31 20 0:31 /batch /sys/fs/cgroup/memory rw - cgroup cgroup rw,memory\n",
    )
    _put(tmp_path, "sys/fs/cgroup/cpuset/job/cpuset.cpus", "\n")
    _put(tmp_path, "sys/fs/cgroup/cpuset/job/cpuset.mems", "\n")
    _put(tmp_path, "sys/fs/cgroup/cpuset/cpuset.cpus", "8,12\n")
    _put(tmp_path, "sys/fs/cgroup/cpuset/cpuset.mems", "1\n")
    _put(
        tmp_path,
        "sys/fs/cgroup/memory/job/memory.limit_in_bytes",
        "9223372036854771712\n",
    )
    _put(tmp_path, "sys/fs/cgroup/memory/memory.limit_in_bytes", "4096\n")

    snapshot = discover_resource_snapshot(
        snapshot_id="v1-synthetic",
        platform_name="linux",
        is_wsl=False,
        paths=_paths(tmp_path),
        affinity_reader=lambda: {8, 12},
        environ={},
    )
    assert snapshot.permitted_cpu_ids == [8, 12]
    assert snapshot.allowed_memory_node_ids == [1]
    assert [(item.state, item.bytes) for item in snapshot.memory_limits] == [
        (LimitState.UNLIMITED, None),
        (LimitState.LIMITED, 4096),
    ]


def test_malformed_and_denied_sources_degrade_with_reasons(tmp_path):
    _put(tmp_path, "sys/devices/system/cpu/online", "4\n")
    _put(
        tmp_path,
        "proc/self/status",
        "Cpus_allowed_list:\t4\nMems_allowed_list:\t0\n",
    )
    _put(tmp_path, "proc/self/cgroup", "0::/bad\n")
    _put(
        tmp_path,
        "proc/self/mountinfo",
        "36 29 0:32 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n",
    )
    bad_cpu = _put(tmp_path, "sys/fs/cgroup/bad/cpuset.cpus.effective", "not-a-list\n")
    denied_mems = _put(tmp_path, "sys/fs/cgroup/bad/cpuset.mems.effective", "0\n")
    _put(tmp_path, "sys/fs/cgroup/bad/memory.max", "many\n")
    _put(tmp_path, "sys/fs/cgroup/cpuset.cpus.effective", "4-5\n")

    def reader(path: Path) -> str:
        if path == denied_mems:
            raise PermissionError("synthetic denial")
        return path.read_text(encoding="utf-8")

    snapshot = discover_resource_snapshot(
        snapshot_id="damaged-synthetic",
        platform_name="linux",
        is_wsl=False,
        paths=_paths(tmp_path),
        affinity_reader=lambda: {4},
        environ={},
        read_text=reader,
    )
    assert snapshot.permitted_cpu_ids is None
    assert snapshot.effective_cpu_ids == [4]
    assert snapshot.allowed_memory_node_ids == [0]
    assert snapshot.memory_limits[0].state == LimitState.UNKNOWN
    assert "invalid memory limit" in snapshot.memory_limits[0].unavailable["bytes"]
    assert "invalid CPU/node list" in snapshot.unavailable["permitted_cpu_ids"]
    assert any("PermissionError" in item for item in snapshot.conflicts)
    assert bad_cpu.read_text() == "not-a-list\n"


def test_missing_cgroup_memory_file_is_an_explicit_unknown_limit(tmp_path):
    paths = _synthetic_v2(tmp_path)
    for path in (
        tmp_path / "sys/fs/cgroup/job/step/memory.max",
        tmp_path / "sys/fs/cgroup/job/memory.max",
        tmp_path / "sys/fs/cgroup/memory.max",
    ):
        path.unlink()
    snapshot = discover_resource_snapshot(
        snapshot_id="missing-limit",
        platform_name="linux",
        is_wsl=False,
        paths=paths,
        affinity_reader=lambda: {2, 6, 10},
        environ={},
    )
    assert len(snapshot.memory_limits) == 1
    limit = snapshot.memory_limits[0]
    assert limit.state == LimitState.UNKNOWN and limit.bytes is None
    assert "memory limit file is missing" in limit.unavailable["bytes"]


def test_cgroup_membership_change_invalidates_discovered_cpuset(tmp_path):
    paths = _synthetic_v2(tmp_path)
    membership_path = tmp_path / "proc/self/cgroup"
    reads = 0

    def reader(path: Path) -> str:
        nonlocal reads
        if path == membership_path:
            reads += 1
            return "0::/tenant/job/step\n" if reads == 1 else "0::/tenant/moved\n"
        return path.read_text(encoding="utf-8")

    snapshot = discover_resource_snapshot(
        snapshot_id="moving-cgroup",
        platform_name="linux",
        is_wsl=False,
        paths=paths,
        affinity_reader=lambda: {2, 6, 10},
        environ={},
        read_text=reader,
    )
    assert snapshot.permitted_cpu_ids is None
    assert snapshot.effective_cpu_ids == [2, 6, 10]
    assert any("membership changed" in item for item in snapshot.conflicts)


def test_empty_affinity_is_known_empty_and_never_replaced_by_dense_count(tmp_path):
    _put(tmp_path, "sys/devices/system/cpu/online", "3,19\n")
    _put(
        tmp_path,
        "proc/self/status",
        "Cpus_allowed_list:\t\nMems_allowed_list:\t\n",
    )
    _put(tmp_path, "proc/self/cgroup", "")
    _put(tmp_path, "proc/self/mountinfo", "")
    snapshot = discover_resource_snapshot(
        snapshot_id="empty-affinity",
        platform_name="linux",
        is_wsl=False,
        paths=_paths(tmp_path),
        affinity_reader=lambda: set(),
        environ={"SLURM_CPUS_PER_TASK": "128"},
    )
    assert snapshot.inherited_cpu_ids == []
    assert snapshot.effective_cpu_ids == []
    assert snapshot.scheduler_hints["SLURM_CPUS_PER_TASK"] == "128"
    assert any("empty intersection" in item for item in snapshot.conflicts)


def test_partial_non_linux_and_wsl_snapshots_do_not_claim_numa_support(tmp_path):
    def should_not_run():
        pytest.fail("non-Linux discovery must not read affinity")

    mac = discover_resource_snapshot(
        snapshot_id="mac-partial",
        platform_name="macos",
        is_wsl=False,
        paths=_paths(tmp_path),
        affinity_reader=should_not_run,
        environ={"SLURM_CPUS_PER_TASK": "4"},
    )
    assert mac.online_cpu_ids is None and mac.effective_cpu_ids is None
    assert mac.scheduler_hints == {"SLURM_CPUS_PER_TASK": "4"}

    paths = _synthetic_v2(tmp_path / "wsl")
    wsl = discover_resource_snapshot(
        snapshot_id="wsl-partial",
        platform_name="linux",
        is_wsl=True,
        paths=paths,
        affinity_reader=lambda: {2, 6, 10},
        environ={},
    )
    assert any("automatic NUMA" in item for item in wsl.conflicts)


def test_optional_hwloc_adapter_only_fills_topology_not_boundaries(tmp_path):
    _put(tmp_path, "sys/devices/system/cpu/online", "2,10\n")
    _put(
        tmp_path,
        "proc/self/status",
        "Cpus_allowed_list:\t2\nMems_allowed_list:\t0\n",
    )
    _put(tmp_path, "proc/self/cgroup", "")
    _put(tmp_path, "proc/self/mountinfo", "")
    xml = """\
<topology>
  <object type="Machine">
    <object type="Package" os_index="0">
      <object type="NUMANode" os_index="0" cpuset="0x80000404" local_memory="4096" />
      <object type="Core" os_index="7">
        <object type="PU" os_index="2" />
        <object type="PU" os_index="10" />
      </object>
      <object type="Core" os_index="9"><object type="PU" os_index="31" /></object>
    </object>
  </object>
</topology>
"""
    calls = []

    def probe(args, timeout=None):
        calls.append((args, timeout))
        return util.CommandResult(0, xml, "")

    snapshot = discover_resource_snapshot(
        snapshot_id="hwloc-synthetic",
        platform_name="linux",
        is_wsl=False,
        paths=_paths(tmp_path),
        affinity_reader=lambda: {2},
        environ={},
        hwloc_adapter=HwlocAdapter("/synthetic/lstopo", probe),
    )
    assert calls == [(["/synthetic/lstopo", "--of", "xml"], 3)]
    assert snapshot.online_cpu_ids == [2, 10]
    assert snapshot.effective_cpu_ids == [2]
    assert [cpu.cpu_id for cpu in snapshot.cpus or []] == [2, 10]
    assert snapshot.cpus and snapshot.cpus[0].smt_siblings == [2, 10]
    assert snapshot.numa_nodes and snapshot.numa_nodes[0].cpu_ids == [2, 10, 31]
    assert "hwloc" in snapshot.sources["cpus"]


@pytest.mark.parametrize(
    "text,expected",
    [("", set()), ("0", {0}), ("2,8-10,31", {2, 8, 9, 10, 31})],
)
def test_cpu_list_parser_preserves_sparse_ids(text, expected):
    assert parse_cpu_list(text) == expected


@pytest.mark.parametrize("text", ["1-0", "-1", "1,,2", "1-", "x", "1:2"])
def test_cpu_list_parser_rejects_malformed_values(text):
    with pytest.raises(ValueError):
        parse_cpu_list(text)


def test_cgroup_parsers_keep_counts_as_text_and_mount_roots_as_paths():
    membership = parse_proc_cgroup("0::/allocation/step\n7:cpu,memory:/job\n")
    assert membership == {
        "__v2__": "/allocation/step",
        "cpu": "/job",
        "memory": "/job",
    }
    mounts = parse_mountinfo("1 0 0:1 /allocation /sys/fs/cgroup rw - cgroup2 cgroup rw\n")
    assert mounts[0].root == "/allocation"
    assert mounts[0].mount_point == "/sys/fs/cgroup"
    with pytest.raises(ValueError):
        parse_proc_cgroup("broken")
    with pytest.raises(ValueError):
        parse_mountinfo("broken")


def test_memory_limit_parser_distinguishes_zero_unlimited_and_malformed():
    assert parse_memory_limit("0", version=2) == (LimitState.LIMITED, 0)
    assert parse_memory_limit("max", version=2) == (LimitState.UNLIMITED, None)
    assert parse_memory_limit(str(1 << 62), version=1) == (LimitState.UNLIMITED, None)
    with pytest.raises(ValueError):
        parse_memory_limit("unknown", version=2)


def test_unexpected_reader_failure_returns_explicit_unknown_snapshot(tmp_path):
    def explode(_path: Path) -> str:
        raise RuntimeError("synthetic adapter failure")

    snapshot = discover_resource_snapshot(
        snapshot_id="failed-adapter",
        platform_name="linux",
        is_wsl=False,
        paths=_paths(tmp_path),
        affinity_reader=lambda: {1},
        environ={},
        read_text=explode,
    )
    assert snapshot.effective_cpu_ids is None
    assert snapshot.online_cpu_ids is None
    assert "RuntimeError" in snapshot.unavailable["online_cpu_ids"]


def test_online_only_discovery_stays_useful_without_affinity_authorization(tmp_path):
    _put(tmp_path, "sys/devices/system/cpu/online", "3,19\n")
    _put(tmp_path, "proc/self/status", "Name:\tpartial-host\n")
    _put(tmp_path, "proc/self/cgroup", "")
    _put(tmp_path, "proc/self/mountinfo", "")

    def unreadable():
        raise OSError("synthetic affinity read unavailable")

    partial = discover_resource_snapshot(
        snapshot_id="online-only",
        platform_name="linux",
        is_wsl=False,
        paths=_paths(tmp_path),
        affinity_reader=unreadable,
        environ={"SLURM_CPUS_PER_TASK": "128"},
    )
    assert partial.online_cpu_ids == partial.effective_cpu_ids == [3, 19]
    assert partial.inherited_cpu_ids is partial.permitted_cpu_ids is None
    assert "synthetic affinity read unavailable" in partial.unavailable["inherited_cpu_ids"]
    assert "inherited_cpu_ids" not in partial.sources["effective_cpu_ids"]
    assert partial.scheduler_hints["SLURM_CPUS_PER_TASK"] == "128"
    # No scheduler-count/dense-mask fallback or discarded partial diagnostic data.
    assert ResourceSnapshot.from_json(partial.to_json()).effective_cpu_ids == [3, 19]
