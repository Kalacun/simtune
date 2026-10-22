"""Stage-06 evidence and applicability checks on synthetic hosts/timings."""

import json
from dataclasses import replace

import pytest
from test_search import CAPABILITIES, FakeClock, SyntheticExecution, make_workload, snapshot

from simtune import cli
from simtune.hardware import HARDWARE_KEY, processor_identity
from simtune.models import CpuTopology, NumaNode, Outcome, Recipe, RuntimeIdentity
from simtune.recipes import RECIPE_KEY, load_recipe, save_recipe, topology_class
from simtune.replay import resolve_recipe, validate_recipe
from simtune.search import SearchConfig, run_search
from simtune.validation import ValidationError
from simtune.workloads import load_workload, sha256_file

incomplete_snapshot = snapshot


@pytest.fixture
def recipe_snapshot(incomplete_snapshot):
    return replace(
        incomplete_snapshot,
        cpus=[
            CpuTopology(cpu_id=i, socket_id=0, core_id=i, smt_siblings=[i], numa_node_id=0)
            for i in (0, 1)
        ],
        numa_nodes=[NumaNode(node_id=0, cpu_ids=[0, 1], memory_bytes=1024**3, distances={"0": 10})],
        allowed_memory_node_ids=[0],
        sources={
            **incomplete_snapshot.sources,
            **{k: "synthetic host only" for k in ("cpus", "numa_nodes", "allowed_memory_node_ids")},
        },
        unavailable={},
        extensions={
            HARDWARE_KEY: processor_identity(
                "processor : 0\nvendor_id : FixtureVendor\nmodel name : FixtureModel\n\n"
                "processor : 1\nvendor_id : FixtureVendor\nmodel name : FixtureModel\n",
                [0, 1],
                "x86_64",
                "synthetic cpuinfo; no hardware claim",
            )
        },
    )


def build_recipe(
    tmp_path, snapshot, *, checked=True, times=None, runtime_known=True, spec_transform=None
):
    loaded = make_workload(tmp_path / "manifest", checked=checked)
    program = tmp_path / "manifest" / "program α.py"
    program.write_text("""#!/usr/bin/env python3
import json, os, signal, sys, time
from pathlib import Path
marker = Path(os.environ["REPLAY_SENTINEL"])
with marker.open("a") as stream:
    stream.write("launch\\n")
mode = os.environ.get("REPLAY_TEST_MODE", "normal")
if mode in ("wait", "ignore"):
    if mode == "ignore":
        for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):
            signal.signal(sig,signal.SIG_IGN)
    ready=marker.parent/"ready.json"
    temporary=ready.with_suffix(".tmp")
    temporary.write_text(json.dumps({"pid":os.getpid(),"pgid":os.getpgrp()}))
    temporary.replace(ready)
    time.sleep(60)
if mode == "input":
    sys.stdout.buffer.write(sys.stdin.buffer.read())
else:
    sys.stdout.buffer.write("out α\\nSTDERR:\\n".encode())
sys.stderr.buffer.write(b"err\\nSTDOUT:\\n")
sys.stdout.flush()
sys.stderr.flush()
Path(sys.argv[2], "result.json").write_text(json.dumps({"science":{"value":6}}))
if mode == "exit":
    sys.exit(17)
if mode == "signal":
    os.kill(os.getpid(), signal.SIGTERM)
""")
    program.chmod(0o755)
    digest = sha256_file(program)
    spec = replace(
        loaded.spec,
        argv_template=[str(program), "{case.input}", "{trial_dir}"],
        resolved_executable=str(program),
        executable_sha256=digest,
        base_directory=str(program.parent),
        artifacts=[replace(loaded.spec.artifacts[0], path=str(program), sha256=digest)],
        fixed_env={"REPLAY_SENTINEL": str(tmp_path / "launches")},
        runtime=RuntimeIdentity(name="synthetic-openmp", version="fixture-1", identity=digest)
        if runtime_known
        else loaded.spec.runtime,
    )
    if spec_transform is not None:
        spec = spec_transform(spec)
    loaded.manifest_path.write_text(spec.to_json())
    loaded = load_workload(loaded.manifest_path)
    clock = FakeClock()
    execution = SyntheticExecution(clock, times=times)
    experiment = tmp_path / "experiment"
    result = run_search(
        loaded,
        experiment,
        SearchConfig(100, bootstrap_samples=100),
        snapshot_provider=lambda: snapshot,
        capabilities=CAPABILITIES,
        clock=clock,
        executor_kwargs=execution.kwargs,
    )
    path = tmp_path / "recipe.json"
    save_recipe(experiment, path)
    return path, loaded, result


@pytest.fixture
def saved(tmp_path, recipe_snapshot):
    return build_recipe(tmp_path, recipe_snapshot)


def test_save_roundtrip_actual_masks_science_and_cost(saved):
    path, workload, experiment = saved
    bundle = load_recipe(path)
    recipe = bundle.recipe
    assert Recipe.from_json(recipe.to_json()) == recipe
    assert recipe.performance_outcome == experiment.outcome == Outcome.VALIDATED_IMPROVEMENT
    assert bundle.details["replay_validated"]
    assert bundle.details["confirmation"]["protocol_valid"]
    assert bundle.details["confirmation"]["protocol_reasons"] == []
    assert recipe.resource_constraints.reference_cpu_ids is None
    assert recipe.evidence_resolutions[0].cpu_ids == [0, 1]
    assert recipe.compatibility.workload_fingerprint == workload.workload_fingerprint
    assert bundle.details["confirmation"]["confirmation"]["lower"] == 2
    assert bundle.details["budget"]["spent_seconds"] == experiment.budget.spent_seconds
    assert bundle.details["plans"][0]["actions"][0]["observed_value"] is None


@pytest.mark.parametrize("version", [0, 2, True])
def test_unsupported_recipe_versions(saved, version):
    path, _, _ = saved
    data = json.loads(path.read_text())
    data["schema_version"] = version
    path.write_text(json.dumps(data))
    with pytest.raises(ValidationError):
        load_recipe(path)


def test_export_preserves_original_and_refuses_overwrite(saved, tmp_path):
    path, _, _ = saved
    before = {p: p.read_bytes() for p in (tmp_path / "experiment").rglob("*") if p.is_file()}
    with pytest.raises(ValidationError, match="already exists"):
        save_recipe(tmp_path / "experiment", path)
    with pytest.raises(ValidationError, match="immutable"):
        save_recipe(tmp_path / "experiment", tmp_path / "experiment" / "recipe.json")
    save_recipe(tmp_path / "experiment", tmp_path / "second.json")
    assert all(p.read_bytes() == value for p, value in before.items())


def test_export_failure_publishes_no_recipe(saved, tmp_path, monkeypatch):
    import simtune.recipes as recipes

    def fail(*a, **k):
        raise OSError("simulated atomic publication failure")

    monkeypatch.setattr(recipes, "_atomic_bytes", fail)
    with pytest.raises(OSError):
        save_recipe(tmp_path / "experiment", tmp_path / "absent.json")
    assert not (tmp_path / "absent.json").exists()
    load_recipe(saved[0])


@pytest.mark.parametrize("tamper", ["bytes", "path", "symlink", "policy", "label", "missing"])
def test_tampered_evidence_fails_closed(saved, tmp_path, tamper):
    path, _, _ = saved
    data = json.loads(path.read_text())
    source = data["evidence"][0]
    if tamper == "bytes":
        (path.parent / source).write_text("{}")
    elif tamper == "path":
        data["evidence"][0] = "../escape"
    elif tamper == "symlink":
        original = path.parent / source
        outside = tmp_path.parent / (tmp_path.name + "-outside")
        outside.write_bytes(original.read_bytes())
        original.unlink()
        original.symlink_to(outside)
    elif tamper == "policy":
        data["policy"]["settings"][0]["value"] = "spread"
    elif tamper == "label":
        data["extensions"][RECIPE_KEY]["replay_validated"] = False
    else:
        (path.parent / source).unlink()
    path.write_text(json.dumps(data))
    with pytest.raises((ValidationError, OSError)):
        load_recipe(path)


def remap(snapshot):
    ids = {0: 12, 1: 30}
    return replace(
        snapshot,
        id="remapped",
        online_cpu_ids=[12, 30],
        inherited_cpu_ids=[12, 30],
        permitted_cpu_ids=[12, 30],
        effective_cpu_ids=[12, 30],
        cpus=[
            replace(
                c,
                cpu_id=ids[c.cpu_id],
                socket_id=9,
                core_id=c.core_id + 7,
                smt_siblings=[ids[c.cpu_id]],
                numa_node_id=6,
            )
            for c in snapshot.cpus
        ],
        numa_nodes=[
            replace(snapshot.numa_nodes[0], node_id=6, cpu_ids=[12, 30], distances={"6": 10})
        ],
        allowed_memory_node_ids=[6],
        extensions={
            HARDWARE_KEY: {
                **snapshot.extensions[HARDWARE_KEY],
                "processors": [
                    dict(p, cpu_id=ids[p["cpu_id"]])
                    for p in snapshot.extensions[HARDWARE_KEY]["processors"]
                ],
            }
        },
    )


def test_new_cpu_node_socket_ids_are_logically_remapped(saved, recipe_snapshot):
    bundle = load_recipe(saved[0])
    changed = remap(recipe_snapshot)
    assert topology_class(changed) == topology_class(recipe_snapshot)
    plan = resolve_recipe(bundle, changed, CAPABILITIES)
    assert list(plan.cpu_ids) == [12, 30]
    assert bundle.recipe.evidence_resolutions[0].cpu_ids == [0, 1]


@pytest.mark.parametrize("change", ["smt", "numa", "narrow", "unknown", "distance"])
def test_cpu_count_is_not_topology_compatibility(saved, recipe_snapshot, change):
    bundle = load_recipe(saved[0])
    s = recipe_snapshot
    if change == "smt":
        s = replace(s, cpus=[replace(c, core_id=0, smt_siblings=[0, 1]) for c in s.cpus])
    elif change == "numa":
        s = replace(
            s,
            cpus=[replace(c, numa_node_id=c.cpu_id) for c in s.cpus],
            numa_nodes=[
                NumaNode(
                    node_id=i,
                    cpu_ids=[i],
                    memory_bytes=512,
                    distances={"0": 10 if i == 0 else 20, "1": 10 if i == 1 else 20},
                )
                for i in (0, 1)
            ],
            allowed_memory_node_ids=[0, 1],
        )
    elif change == "narrow":
        s = replace(s, inherited_cpu_ids=[0], effective_cpu_ids=[0])
    elif change == "unknown":
        s = replace(s, cpus=None, unavailable={"cpus": "missing observation"})
    else:
        s = replace(s, numa_nodes=[replace(s.numa_nodes[0], distances={"0": 99})])
    with pytest.raises(ValidationError, match="topology"):
        resolve_recipe(bundle, s, CAPABILITIES)


@pytest.mark.parametrize(
    "change",
    ["family", "source", "binary", "input", "runtime", "resources", "fixed_env", "structure"],
)
def test_changed_identity_requires_explicit_validation(saved, recipe_snapshot, change):
    bundle = load_recipe(saved[0])
    spec = bundle.workload
    if change == "family":
        spec = replace(spec, family="different")
    elif change == "source":
        spec = replace(spec, artifacts=[replace(spec.artifacts[0], sha256="f" * 64)])
    elif change == "binary":
        spec = replace(spec, executable_sha256="e" * 64)
    elif change == "input":
        spec = replace(spec, cases=[replace(c, input_class="different") for c in spec.cases])
    elif change == "runtime":
        spec = replace(spec, runtime=replace(spec.runtime, version="fixture-2"))
    elif change == "resources":
        spec = replace(
            spec,
            cases=[
                replace(c, resource_needs=replace(c.resource_needs, thread_count=1))
                for c in spec.cases
            ],
        )
    elif change == "fixed_env":
        spec = replace(spec, fixed_env={**spec.fixed_env, "SCIENCE_PRECISION": "different"})
    else:
        spec = replace(spec, argv_template=[*spec.argv_template, "--different-science"])
    with pytest.raises(ValidationError, match="incompatible"):
        resolve_recipe(bundle, recipe_snapshot, CAPABILITIES, workload=spec)


def test_unchecked_and_missing_runtime_are_explicit(tmp_path, recipe_snapshot):
    for name, kwargs in (("unchecked", {"checked": False}), ("runtime", {"runtime_known": False})):
        path, _, _ = build_recipe(tmp_path / name, recipe_snapshot, **kwargs)
        bundle = load_recipe(path)
        assert not bundle.details["replay_validated"]
        assert bundle.details["limitations"]


def test_no_improvement_reference_recipe_is_honest(tmp_path, recipe_snapshot):
    path, _, _ = build_recipe(
        tmp_path, recipe_snapshot, times={"baseline": 1, "runtime-close": 2, "runtime-spread": 2}
    )
    bundle = load_recipe(path)
    assert bundle.recipe.policy.id == "baseline"
    assert bundle.recipe.performance_outcome == Outcome.NO_IMPROVEMENT
    assert not bundle.details["heldout_confirmed"]
    assert not bundle.details["replay_validated"]
    assert bundle.details["break_even_runs"] is None


def test_explain_human_json_same_facts_no_launch(saved, monkeypatch, capsys):
    def forbidden(*a, **k):
        pytest.fail("explain launched a program")

    monkeypatch.setattr("simtune.profiler.execute_command", forbidden)
    assert cli.main(["explain", str(saved[0]), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["label"] == "validated"
    assert data["measured_evidence"]["confirmation"]["confirmation"]["lower"] == 2
    assert cli.main(["explain", str(saved[0])]) == 0
    human = capsys.readouterr().out
    for text in (
        "runtime-close",
        "OMP_PROC_BIND",
        "Cost:",
        "Observed:",
        "Limitation:",
        "input classes",
    ):
        assert text in human


@pytest.mark.parametrize("failure", [False, True])
def test_revalidation_preserves_source_and_uses_only_fresh_pairs(
    saved, recipe_snapshot, tmp_path, failure
):
    path, workload, _ = saved
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    changed = replace(workload.spec, runtime=replace(workload.spec.runtime, version="fixture-2"))
    manifest = tmp_path / "updated.json"
    manifest.write_text(changed.to_json())
    clock = FakeClock()
    execution = SyntheticExecution(
        clock,
        times={"baseline": 2, "recipe-policy": 1},
        failed={"recipe-policy"} if failure else (),
    )
    result, derived = validate_recipe(
        path,
        manifest,
        tmp_path / "validation",
        config=SearchConfig(100, bootstrap_samples=100),
        snapshot_provider=lambda: recipe_snapshot,
        capabilities=CAPABILITIES,
        clock=clock,
        executor_kwargs=execution.kwargs,
    )
    assert result.outcome == (Outcome.INCONCLUSIVE if failure else Outcome.VALIDATED_IMPROVEMENT)
    assert {call["policy"] for call in execution.calls} <= {"baseline", "recipe-policy"}
    assert all(p.read_bytes() == content for p, content in before.items())
    new = load_recipe(derived)
    assert new.recipe.compatibility.runtime.version == "fixture-2"
    assert new.details["source_recipe"]["sha256"] == sha256_file(path)
    assert new.details["replay_validated"] is (not failure)
    assert new.details["experiment_id"] != load_recipe(path).details["experiment_id"]


def test_revalidation_confirms_even_without_training_gain(saved, recipe_snapshot, tmp_path):
    path, workload, _ = saved
    clock = FakeClock()
    execution = SyntheticExecution(clock, times={"baseline": 1, "recipe-policy": 1})
    result, derived = validate_recipe(
        path,
        workload.manifest_path,
        tmp_path / "equal",
        config=SearchConfig(100, bootstrap_samples=100),
        snapshot_provider=lambda: recipe_snapshot,
        capabilities=CAPABILITIES,
        clock=clock,
        executor_kwargs=execution.kwargs,
    )
    assert result.outcome == Outcome.NO_IMPROVEMENT
    bundle = load_recipe(derived)
    assert bundle.recipe.policy.id == "baseline"
    assert bundle.details["heldout_confirmed"] and bundle.details["replay_validated"]
    assert bundle.details["confirmation"]["protocol_valid"]
    assert bundle.details["confirmation"]["protocol_reasons"] == []
    assert len(result.confirmation_trial_ids) == 10


def test_missing_numa_tool_rejects_recipe_without_launch(
    saved, recipe_snapshot, tmp_path, monkeypatch
):
    from simtune.launch import StrictPlanningError
    from simtune.replay import exec_recipe

    _, workload, _ = saved
    caps = {
        "platform": "linux",
        "tools": {"numactl": {"available": True, "path": "/synthetic/numactl"}},
    }
    clock = FakeClock()
    execution = SyntheticExecution(
        clock, times={"baseline": 2, "runtime-close": 2, "runtime-spread": 2, "numa-local": 1}
    )
    root = tmp_path / "numa-evidence"
    run_search(
        workload,
        root,
        SearchConfig(100, bootstrap_samples=100),
        snapshot_provider=lambda: recipe_snapshot,
        capabilities=caps,
        clock=clock,
        executor_kwargs=execution.kwargs,
    )
    path = tmp_path / "numa.json"
    save_recipe(root, path)
    assert load_recipe(path).recipe.compatibility.required_capabilities == ["numactl"]
    monkeypatch.setattr(
        "simtune.replay.execute_strict_launch", lambda *a, **k: pytest.fail("launched")
    )
    with pytest.raises(StrictPlanningError, match="numactl"):
        exec_recipe(
            path,
            [workload.spec.resolved_executable, "training-data", str(tmp_path)],
            cwd=tmp_path,
            snapshot_provider=lambda: recipe_snapshot,
            capabilities=CAPABILITIES,
            allow_unvalidated=True,
        )


def test_required_skip_and_changed_launch_measurement_cannot_be_exported(saved, tmp_path):
    from simtune.search import STATE_KEY

    root = tmp_path / "experiment"
    experiment = json.loads((root / "experiment.json").read_text())
    selected = experiment["selected_policy_id"]
    trial_id = next(
        item["id"]
        for item in experiment["extensions"][STATE_KEY]["schedule"]
        if item["policy_id"] == selected
    )
    plan_path = root / "trials" / trial_id / "plan.json"
    original = plan_path.read_bytes()
    plan = json.loads(original)
    plan["actions"][0]["status"] = "skipped"
    plan_path.write_text(json.dumps(plan))
    with pytest.raises(ValidationError, match="required setting"):
        save_recipe(root, tmp_path / "skip.json")
    plan_path.write_bytes(original)
    launch = root / "trials" / trial_id / "launch.json"
    data = json.loads(launch.read_text())
    data["metrics"]["wall_time_sec"] = 123456
    launch.write_text(json.dumps(data))
    with pytest.raises(ValidationError, match="measurement"):
        save_recipe(root, tmp_path / "tampered-measurement.json")
    # Already exported evidence is independent of later source damage.
    assert load_recipe(saved[0]).details["replay_validated"]


def test_inherited_runtime_control_changes_cannot_be_overridden(
    saved, recipe_snapshot, monkeypatch
):
    bundle = load_recipe(saved[0])
    monkeypatch.setenv("OMP_PLACES", "threads")
    with pytest.raises(ValidationError, match="inherited runtime settings"):
        resolve_recipe(bundle, recipe_snapshot, CAPABILITIES)


def test_memory_constraints_checked_independently_of_topology(saved, recipe_snapshot):
    from simtune.launch import StrictPlanningError
    from simtune.models import LimitState, MemoryLimit

    bundle = load_recipe(saved[0])
    # Exercise the resolver's resource boundary independently of export, whose
    # evidence tests above forbid modifying serialized policies.
    budget = replace(
        bundle.recipe.policy.budget,
        memory_bytes=4096,
        unavailable={"reference_cpu_ids": "logical reference"},
    )
    policy = replace(bundle.recipe.policy, budget=budget)
    recipe = replace(bundle.recipe, policy=policy, resource_constraints=budget)
    workload = replace(
        bundle.workload, cases=[replace(c, resource_needs=budget) for c in bundle.workload.cases]
    )
    bundle = replace(bundle, recipe=recipe, workload=workload)
    s = replace(
        recipe_snapshot,
        memory_limits=[
            MemoryLimit(
                source="ancestor fixture", scope="allocation", state=LimitState.LIMITED, bytes=1024
            )
        ],
    )
    with pytest.raises(StrictPlanningError, match="1024 bytes"):
        resolve_recipe(bundle, s, CAPABILITIES)
    # Unknown caps remain unknown for inherited NUMA, as in fresh search planning.
    plan = resolve_recipe(bundle, recipe_snapshot, CAPABILITIES)
    assert any("neither reserved memory" in reason for reason in plan.unobserved)


def test_revalidation_dryrun_and_missing_checks_launch_nothing(
    saved, recipe_snapshot, tmp_path, monkeypatch
):
    path, workload, _ = saved
    monkeypatch.setattr("simtune.profiler.execute_command", lambda *a, **k: pytest.fail("launched"))
    result, derived = validate_recipe(
        path,
        workload.manifest_path,
        tmp_path / "absent",
        dry_run=True,
        snapshot_provider=lambda: recipe_snapshot,
        capabilities=CAPABILITIES,
    )
    assert result["dry_run"] and derived is None and not (tmp_path / "absent").exists()
    spec = replace(workload.spec, output_checks=[])
    missing = tmp_path / "missing.json"
    missing.write_text(spec.to_json())
    with pytest.raises(ValidationError, match="scientific checks"):
        validate_recipe(path, missing, tmp_path / "also-absent")
    assert not (tmp_path / "also-absent").exists()


def test_atomic_export_does_not_replace_a_racing_destination(saved, tmp_path, monkeypatch):
    import simtune.recipes as recipes

    target = tmp_path / "racing.json"
    original = recipes._atomic_bytes

    def concurrent_writer(path, payload):
        original(path, payload)
        target.write_text("keep this concurrently created file")

    monkeypatch.setattr(recipes, "_atomic_bytes", concurrent_writer)
    with pytest.raises(FileExistsError):
        save_recipe(tmp_path / "experiment", target)
    assert target.read_text() == "keep this concurrently created file"


def test_tune_save_cli_uses_real_search_records(
    saved, recipe_snapshot, tmp_path, monkeypatch, capsys
):
    import simtune.tune as tune

    _, workload, _ = saved
    clock = FakeClock()
    execution = SyntheticExecution(clock)
    original = tune.run_search

    def measured(*args, **kwargs):
        return original(
            *args,
            **kwargs,
            snapshot_provider=lambda: recipe_snapshot,
            capabilities=CAPABILITIES,
            clock=clock,
            executor_kwargs=execution.kwargs,
        )

    monkeypatch.setattr(tune, "run_search", measured)
    target = tmp_path / "cli-recipe.json"
    assert (
        cli.main(
            [
                "tune",
                "--workload",
                str(workload.manifest_path),
                "--budget",
                "100s",
                "--out",
                str(tmp_path / "cli-search"),
                "--save",
                str(target),
            ]
        )
        == 0
    )
    assert load_recipe(target).details["replay_validated"]
    assert "Recipe saved:" in capsys.readouterr().out


def build_protocol_recipe(tmp_path, snapshot, monkeypatch, problem):
    """Produce retained, invalid evidence through the real search/trial pipeline."""
    from simtune import search
    from simtune.models import Artifact, Phase

    if problem in {"early_confirmation", "insufficient_pairs"}:
        schedule = search.paired_schedule

        def invalid_schedule(**kwargs):
            if kwargs["phase"] == Phase.CONFIRMATION:
                requests = schedule(**kwargs)
                if problem == "early_confirmation":
                    return [replace(r, order=r.order - kwargs["start_order"]) for r in requests]
                return requests[:8] if problem == "insufficient_pairs" else requests
            return schedule(**kwargs)

        monkeypatch.setattr(search, "paired_schedule", invalid_schedule)

    def transform(spec):
        train, heldout = spec.cases
        if problem == "reused_inputs":
            heldout = replace(heldout, arguments=train.arguments)
        elif problem == "renamed_artifacts":
            cases = []
            for case in (train, heldout):
                path = tmp_path / (case.id + ".txt")
                path.write_text("identical scientific input bytes")
                cases.append(
                    replace(
                        case,
                        arguments={"input": str(path)},
                        input_artifacts=[
                            Artifact(
                                id="input", role="input", path=str(path), sha256=sha256_file(path)
                            )
                        ],
                    )
                )
            train, heldout = cases
        elif problem in {"unchecked", "failed_checks"}:
            train = replace(train, checks=spec.output_checks)
            heldout = replace(
                heldout,
                checks=[]
                if problem == "unchecked"
                else [replace(spec.output_checks[0], expected=999)],
            )
            spec = replace(spec, output_checks=[])
        return replace(spec, cases=[train, heldout])

    return build_recipe(tmp_path, snapshot, spec_transform=transform)


def refresh_completion(root):
    """Keep deliberately constructed test records complete, without running work."""
    marker = root / "completion.json"
    data = json.loads(marker.read_text())
    data["experiment_record_sha256"] = sha256_file(root / "experiment.json")
    marker.write_text(json.dumps(data))


def legacy_recipe(path):
    """Encode the documented pre-R01 extension and unversioned result shape."""
    from simtune.search import STATE_KEY

    data = json.loads(path.read_text())
    details = data["extensions"][RECIPE_KEY]
    details["schema_version"] = 1
    details.pop("hardware", None)
    details.pop("label_scope", None)
    if details["confirmation"] is not None:
        del details["confirmation"]["protocol_valid"]
        del details["confirmation"]["protocol_reasons"]
    experiment = path.parent / details["evidence_root"] / "experiment.json"
    source = json.loads(experiment.read_text())
    confirmation = source["extensions"][STATE_KEY].get("confirmation_result")
    if confirmation is not None:
        del confirmation["protocol_valid"]
        del confirmation["protocol_reasons"]
        experiment.write_text(json.dumps(source))
        refresh_completion(experiment.parent)
        details["sha256"][f"{details['evidence_root']}/experiment.json"] = sha256_file(experiment)
        details["sha256"][f"{details['evidence_root']}/completion.json"] = sha256_file(
            experiment.parent / "completion.json"
        )
    path.write_text(json.dumps(data))


@pytest.mark.parametrize("outcome", ["improvement", "no_improvement", "unconfirmed"])
def test_legacy_extension_is_explicitly_recomputed(tmp_path, recipe_snapshot, outcome):
    def timing(policy, case):
        return 1 if policy == "runtime-close" and case == "training-data" else 2

    kwargs = {}
    if outcome == "no_improvement":
        kwargs["times"] = timing
    elif outcome == "unconfirmed":
        kwargs["times"] = {"baseline": 2, "runtime-close": 2, "runtime-spread": 2}
    path, _, _ = build_recipe(tmp_path, recipe_snapshot, **kwargs)
    legacy_recipe(path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    bundle = load_recipe(path)
    assert bundle.recipe.extensions[RECIPE_KEY]["schema_version"] == 1
    assert bundle.details["heldout_confirmed"] is (outcome != "unconfirmed")
    assert bundle.details["replay_validated"] is (outcome != "unconfirmed")
    if outcome != "unconfirmed":
        assert "protocol_valid" not in bundle.recipe.extensions[RECIPE_KEY]["confirmation"]
        assert bundle.details["confirmation"]["protocol_valid"]
    assert all(p.read_bytes() == value for p, value in before.items())


@pytest.mark.parametrize("boundary", ["export", "reload"])
def test_reused_trial_evidence_cannot_be_upgraded(saved, tmp_path, boundary):
    from simtune.models import Phase, TrialRecord

    path, _, _ = saved
    data = json.loads(path.read_text())
    details = data["extensions"][RECIPE_KEY]
    root = tmp_path / ("experiment" if boundary == "export" else details["evidence_root"])
    trials = [TrialRecord.from_json(p.read_text()) for p in (root / "trials").glob("*/trial.json")]
    training = next(t for t in trials if t.phase == Phase.TRAINING)
    confirmation = next(t for t in trials if t.phase == Phase.CONFIRMATION)
    damaged = replace(
        confirmation,
        metrics=[replace(confirmation.metrics[0], raw_source=training.metrics[0].raw_source)],
    )
    trial_path = root / "trials" / confirmation.id / "trial.json"
    trial_path.write_text(damaged.to_json())
    if boundary == "reload":
        reference = str(trial_path.relative_to(tmp_path))
        details["sha256"][reference] = sha256_file(trial_path)
        path.write_text(json.dumps(data))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(ValidationError, match="reused evidence"):
        if boundary == "export":
            save_recipe(root, tmp_path / "reused.json")
        else:
            load_recipe(path)
    assert all(p.read_bytes() == value for p, value in before.items())


def test_export_recomputes_protocol_instead_of_trusting_search_summary(
    tmp_path, recipe_snapshot, monkeypatch
):
    from simtune.search import STATE_KEY

    build_protocol_recipe(tmp_path, recipe_snapshot, monkeypatch, "reused_inputs")
    root = tmp_path / "experiment"
    experiment = root / "experiment.json"
    data = json.loads(experiment.read_text())
    data["extensions"][STATE_KEY]["confirmation_result"].update(
        protocol_valid=True, protocol_reasons=[]
    )
    experiment.write_text(json.dumps(data))
    refresh_completion(root)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    path = tmp_path / "recomputed.json"
    recipe = save_recipe(root, path)
    assert not recipe.extensions[RECIPE_KEY]["heldout_confirmed"]
    bundle = load_recipe(path)
    assert not bundle.details["confirmation"]["protocol_valid"]
    assert not bundle.details["replay_validated"]
    assert all(p.read_bytes() == value for p, value in before.items())
