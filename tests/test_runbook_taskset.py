"""Test the documented function alone; never execute runbook workload blocks."""

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

RUNBOOK = Path(__file__).resolve().parents[1] / "docs/implementation/release-plan/r06/RUNBOOK.md"


def documented_restricted(taskset, mask):
    functions = []
    for block in re.findall(r"^```python\n(.*?)^```", RUNBOOK.read_text(), re.M | re.S):
        functions.extend(
            node
            for node in ast.parse(block).body
            if isinstance(node, ast.FunctionDef) and node.name == "restricted"
        )
    assert len(functions) == 1, "the runbook must have one authoritative restricted function"
    function = functions[0]
    # Reject decorators, defaults, annotations, imports and side effects. Compile
    # only this function, with only map/str available; no other block is executed.
    assert ast.dump(function.args) == ast.dump(
        ast.arguments(
            posonlyargs=[],
            args=[ast.arg(arg="argv")],
            kwonlyargs=[],
            kw_defaults=[],
            defaults=[],
        )
    )
    assert not function.decorator_list and function.returns is None
    assert len(function.body) == 1 and isinstance(function.body[0], ast.Return)
    value = function.body[0].value
    assert isinstance(value, ast.List) and len(value.elts) == 5
    assert all(isinstance(item, (ast.Name, ast.Constant)) for item in value.elts[:4])
    assert all(
        (isinstance(item, ast.Name) and item.id in {"taskset", "mask"})
        or (isinstance(item, ast.Constant) and item.value in {"-c", "--"})
        for item in value.elts[:4]
    )
    assert ast.dump(value.elts[4]) == ast.dump(
        ast.parse("[*map(str, argv)]", mode="eval").body.elts[0]
    )
    namespace = {"__builtins__": {"map": map, "str": str}, "taskset": taskset, "mask": mask}
    module = ast.Module(body=[function], type_ignores=[])
    exec(compile(module, str(RUNBOOK), "exec"), namespace)
    return namespace["restricted"]


@pytest.mark.parametrize("mask", ["2,6", "10,2,6", "6"])
def test_documented_taskset_order_preserves_selected_mask(mask):
    restricted = documented_restricted("/tool path/taskset", mask)
    argv = [Path("/command path/python"), "--leading", "$HOME; $(literal)", "space argument"]
    original = argv.copy()
    assert restricted(argv) == ["/tool path/taskset", "-c", "--", mask, *map(str, argv)]
    assert argv == original


def test_documented_taskset_literal_arguments(tmp_path):
    # A harmless argv recorder, explicitly synthetic: no Linux/taskset claim.
    tool = tmp_path / "synthetic taskset with spaces"
    tool.write_text(f"#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
    tool.chmod(0o700)
    marker = tmp_path / "must not exist"
    arguments = [
        Path("/command path with spaces/python"),
        "space argument",
        "--leading-hyphen",
        "-c",
        "--",
        "$HOME",
        f"$(touch {marker})",
        f"; touch {marker}",
        "`literal` | > * &",
        "quote'\" and newline\nargument",
    ]
    command = documented_restricted(str(tool), "2,6")(arguments)
    result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["-c", "--", "2,6", *map(str, arguments)]
    assert not marker.exists()


def test_native_linux_documented_taskset(tmp_path):
    if sys.platform != "linux":
        pytest.skip(f"actual Linux required; current platform is {sys.platform}")
    taskset = shutil.which("taskset")
    if taskset is None:
        pytest.skip("taskset is unavailable on the actual Linux host")
    if not hasattr(os, "sched_getaffinity"):
        pytest.skip("sched_getaffinity is unavailable on the actual Linux host")
    try:
        parent_before = sorted(os.sched_getaffinity(0))
    except OSError as exc:
        pytest.skip(f"parent affinity cannot be observed: {exc}")
    if not parent_before:
        pytest.skip("observed parent affinity is empty")
    # Select only observed permitted IDs, prefer a sparse pair when available.
    selected = parent_before[:1] if len(parent_before) < 3 else [parent_before[0], parent_before[2]]
    mask = ",".join(map(str, selected))
    python = tmp_path / "python with spaces"
    python.symlink_to(sys.executable)
    arguments = ["space argument", "--leading", "$HOME; $(literal)", "`literal` | > * &"]
    command = documented_restricted(taskset, mask)(
        [
            python,
            "-c",
            "import json,os,sys; print(json.dumps([sorted(os.sched_getaffinity(0)),sys.argv[1:]]))",
            *arguments,
        ]
    )
    evidence = {
        "platform": sys.platform,
        "runbook_sha256": hashlib.sha256(RUNBOOK.read_bytes()).hexdigest(),
        "taskset_path": taskset,
        "taskset_resolved": str(Path(taskset).resolve()),
        "taskset_sha256": hashlib.sha256(Path(taskset).read_bytes()).hexdigest(),
        "parent_before": parent_before,
        "selected": selected,
        "argv": command,
        "timeout_seconds": 5,
    }
    try:
        version = subprocess.run(
            [taskset, "--version"], capture_output=True, text=True, timeout=5, check=False
        )
        evidence["tool_version"] = {
            "stdout": version.stdout,
            "stderr": version.stderr,
            "exit_status": version.returncode,
        }
        result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
        evidence.update(stdout=result.stdout, stderr=result.stderr, exit_status=result.returncode)
        assert result.returncode == 0, evidence
        observed = json.loads(result.stdout)
        evidence["child_observed"] = observed
        assert observed == [selected, arguments]
    except subprocess.TimeoutExpired as exc:
        evidence.update(
            state="timed_out",
            stdout=(exc.stdout or b"").decode(errors="replace"),
            stderr=(exc.stderr or b"").decode(errors="replace"),
            exit_status=None,
        )
        raise
    finally:
        evidence["parent_after"] = sorted(os.sched_getaffinity(0))
        (tmp_path / "native-taskset.json").write_text(json.dumps(evidence, indent=2) + "\n")
        assert evidence["parent_after"] == parent_before
