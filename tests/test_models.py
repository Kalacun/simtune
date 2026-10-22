"""Synthetic format tests. No topology collection, search, or performance claims."""

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from simtune import models as m
from simtune.validation import ValidationError, artifact_path

FIXTURES = Path(__file__).parent / "fixtures" / "extension"


def fixture(name):
    return json.loads((FIXTURES / "valid" / f"{name}.json").read_text())


@pytest.mark.parametrize(
    "name", ["resource", "case", "workload", "policy", "trial", "experiment", "recipe", "metric"]
)
def test_record_roundtrip_and_envelopes(name, tmp_path):
    data = fixture(name)
    record = m.record_from_dict(data, root=tmp_path)
    assert record.to_dict(root=tmp_path) == data
    assert m.record_from_json(record.to_json(root=tmp_path), root=tmp_path) == record
    assert type(record).from_json(record.to_json()) == record
    assert record.KIND == data["kind"] and record.SCHEMA_VERSION == 1


@pytest.mark.parametrize(
    "path", sorted((FIXTURES / "invalid").glob("*.json")), ids=lambda p: p.name
)
def test_invalid_json_fixtures(path):
    with pytest.raises(ValidationError):
        m.record_from_json(path.read_text())


def test_toml_matches_json_and_invalid_fixture():
    record = m.WorkloadSpec.from_toml((FIXTURES / "valid/workload.toml").read_text())
    assert record.to_dict() == m.record_from_dict(fixture("workload")).to_dict()
    with pytest.raises(ValidationError):
        m.WorkloadSpec.from_toml((FIXTURES / "invalid/bool-weight.toml").read_text())
    with pytest.raises(ValidationError, match="TOML"):
        m.WorkloadSpec.from_toml("[[invalid")


@pytest.mark.parametrize("version", [True, False, 0, 2, "1", 1.0, None])
def test_version_is_independent_and_exact(version):
    data = fixture("metric")
    data["schema_version"] = version
    with pytest.raises(ValidationError, match="schema_version"):
        m.record_from_dict(data)


def test_unknown_kind_and_wrong_record_class():
    with pytest.raises(ValidationError, match="kind"):
        m.Recipe.from_dict(fixture("trial"))
    with pytest.raises(ValidationError, match="kind"):
        m.record_from_dict({"kind": [], "schema_version": 1})
    with pytest.raises(ValidationError, match="kind"):
        m.record_from_dict({"schema_version": 2, "mode": "baseline"})


@pytest.mark.parametrize(
    "name", ["resource", "case", "workload", "policy", "trial", "experiment", "recipe", "metric"]
)
def test_all_wire_fields_required_except_explicit_maps(name):
    data = fixture(name)
    for key in data.keys() - {"extensions", "unavailable"}:
        missing = copy.deepcopy(data)
        del missing[key]
        with pytest.raises(ValidationError, match=key if key != "id" else "id"):
            m.record_from_dict(missing)
    data["typo"] = "never silently ignored"
    with pytest.raises(ValidationError, match="unknown fields"):
        m.record_from_dict(data)


@pytest.mark.parametrize("value", [True, False, float("nan"), float("inf"), float("-inf"), "1"])
def test_finite_values_and_booleans_excluded(value):
    data = fixture("case")
    data["weight"] = value
    with pytest.raises(ValidationError):
        m.record_from_dict(data)
    data = fixture("metric")
    data["value"] = value
    data["unavailable"].pop("value")
    with pytest.raises(ValidationError):
        m.record_from_dict(data)


@pytest.mark.parametrize(
    "text",
    ['{"kind":"metric","kind":"recipe"}', '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}', '{"x":'],
)
def test_invalid_json_never_becomes_success(text):
    with pytest.raises(ValidationError):
        m.record_from_json(text)


@pytest.mark.parametrize(
    "value", ["", " ", "../evil", "/root", "a/b", "a\\b", ".", "..", "α", "a\n", "x" * 129]
)
def test_safe_ids(value):
    data = fixture("case")
    data["id"] = value
    with pytest.raises(ValidationError):
        m.record_from_dict(data)


def test_duplicate_case_metric_setting_and_cpu_ids():
    for name, key in [
        ("workload", "cases"),
        ("trial", "metrics"),
        ("policy", "settings"),
        ("resource", "cpus"),
    ]:
        data = fixture(name)
        data[key].append(copy.deepcopy(data[key][0]))
        with pytest.raises(ValidationError, match="duplicate"):
            m.record_from_dict(data)
    data = fixture("workload")
    data["training_case_ids"] *= 2
    with pytest.raises(ValidationError, match="duplicate"):
        m.record_from_dict(data)


@pytest.mark.parametrize(
    "reference",
    [
        "../escape",
        "dir/../../escape",
        "/absolute",
        "",
        ".",
        "a/./b",
        "a//b",
        "a\\b",
        "C:/escape",
        "a\0b",
        "a\nb",
    ],
)
def test_artifacts_reject_lexical_escape(reference, tmp_path):
    with pytest.raises(ValidationError):
        artifact_path(tmp_path, reference)
    data = fixture("trial")
    data["raw_artifacts"] = [reference]
    with pytest.raises(ValidationError):
        m.record_from_dict(data)


def test_artifacts_check_symlink_ancestors_and_missing_leaves(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    (root / "broken").symlink_to(outside / "missing")
    (root / "loop").symlink_to(root / "loop")
    for ref in ("escape/missing.txt", "broken", "loop"):
        with pytest.raises(ValidationError):
            artifact_path(root, ref)
        data = fixture("trial")
        data["raw_artifacts"] = [ref]
        with pytest.raises(ValidationError):
            m.record_from_dict(data, root=root)
    (root / "inside").mkdir()
    (root / "alias").symlink_to(root / "inside", target_is_directory=True)
    assert artifact_path(root, "alias/new.txt") == root / "inside/new.txt"
    assert artifact_path(root, "output α $ data.txt") == root / "output α $ data.txt"


def test_argv_literals_and_env_preserved(tmp_path):
    data = fixture("trial")
    data["argv"] = [
        "/synthetic/solver",
        "",
        "α space",
        "$HOME",
        "line\nbreak",
        "--repeat",
        "3",
        "`literal`",
    ]
    data["env_overrides"] = {"VALUE": "$HOME\nα"}
    record = m.record_from_dict(data)
    assert m.record_from_json(record.to_json()).argv == data["argv"]
    assert record.env_overrides == data["env_overrides"]
    for argv in ([], [""], ["solver", "nul\0"]):
        with pytest.raises(ValidationError):
            replace(record, argv=argv)
    with pytest.raises(ValidationError):
        replace(record, env_overrides={"BAD=KEY": "value"})


def test_unknown_needs_reason_zero_is_known_and_success_is_explicit():
    data = fixture("metric")
    data["unavailable"].pop("value")
    with pytest.raises(ValidationError, match="unavailable"):
        m.record_from_dict(data)
    data["value"] = 0
    assert m.record_from_dict(data).value == 0
    data["unavailable"]["value"] = "contradictory"
    with pytest.raises(ValidationError):
        m.record_from_dict(data)
    trial = fixture("trial")
    trial["status"] = "succeeded"
    with pytest.raises(ValidationError, match="success"):
        m.record_from_dict(trial)
    for status in ("unknown", "interrupted", "unsupported"):
        trial["status"] = status
        assert m.record_from_dict(trial).status.value == status


def test_direct_constructor_and_mutated_nested_data_are_validated():
    case = m.record_from_dict(fixture("case"))
    with pytest.raises(ValidationError):
        replace(case, weight=True)
    case.extensions["bad"] = float("nan")
    with pytest.raises(ValidationError):
        case.to_json()
    data = fixture("case")
    data["extensions"] = {"vendor": {"toggle": True, "number": 2, "unknown": None}}
    assert m.record_from_dict(data).to_dict() == data
    data["resource_needs"]["memroy"] = 2
    with pytest.raises(ValidationError, match="memroy"):
        m.record_from_dict(data)


def test_boundaries_no_guessed_cpus_and_memory_independent():
    data = fixture("resource")
    data["inherited_cpu_ids"] = [6]
    with pytest.raises(ValidationError, match="intersection"):
        m.record_from_dict(data)
    data["effective_cpu_ids"] = [6]
    record = m.record_from_dict(data)
    assert record.allowed_memory_node_ids == [0]
    assert record.memory_limits[0].bytes is None
    data["inherited_cpu_ids"] = []
    data["effective_cpu_ids"] = []
    assert m.record_from_dict(data).effective_cpu_ids == []
    for key in ("online_cpu_ids", "inherited_cpu_ids"):
        data[key] = None
        data["unavailable"][key] = "Not discovered"
    with pytest.raises(ValidationError, match="known boundary"):
        m.record_from_dict(data)


def test_workload_placeholders_partition_and_checks():
    for arg in ("prefix{case.input}", "{case.unknown}", "{case.input.__class__}", "{env.HOME}"):
        data = fixture("workload")
        data["argv_template"][1] = arg
        with pytest.raises(ValidationError, match="placeholder"):
            m.record_from_dict(data)
    data = fixture("workload")
    data["validation_case_ids"] = data["training_case_ids"]
    with pytest.raises(ValidationError, match="disjoint"):
        m.record_from_dict(data)


def test_checks_finite_tolerance_and_validation_not_exit_success():
    data = dict(
        id="numeric",
        method="numeric_json",
        output="result.json",
        sha256=None,
        json_field=["science", "value"],
        expected=0.0,
        absolute_tolerance=0.01,
        relative_tolerance=0.0,
        argv=None,
        timeout_seconds=None,
        rationale="Compare fixed scientific reference.",
        unavailable={k: "Not applicable" for k in ("sha256", "argv", "timeout_seconds")},
    )
    check = m.OutputCheck.from_dict(data)
    assert check.expected == 0
    with pytest.raises(ValidationError):
        replace(check, absolute_tolerance=-1)
    with pytest.raises(ValidationError):
        replace(check, expected=True)
    validation = m.record_from_dict(fixture("trial")).validation
    with pytest.raises(ValidationError, match="checks and evidence"):
        replace(validation, state=m.ValidationState.PASSED)


def test_selection_contract_exclusions_and_disjoint_evidence():
    data = fixture("experiment")
    data["training_trial_ids"] = ["same"]
    data["confirmation_trial_ids"] = ["same"]
    with pytest.raises(ValidationError, match="disjoint"):
        m.record_from_dict(data)
    record = m.record_from_dict(fixture("experiment"))
    with pytest.raises(ValidationError, match="5 confirmation"):
        replace(record.selection_rule, minimum_confirmation_pairs=4)
    with pytest.raises(ValidationError, match="validated outcome"):
        replace(record, outcome=m.Outcome.VALIDATED_IMPROVEMENT)
    data = fixture("experiment")
    data["candidates"][0]["exclusion_reason"] = "Unsupported"
    data["candidates"][0]["unavailable"] = {}
    data["selected_policy_id"] = "spread"
    del data["unavailable"]["selected_policy_id"]
    with pytest.raises(ValidationError, match="excluded"):
        m.record_from_dict(data)


def test_recipe_cannot_claim_validated_improvement_from_unchecked_evidence():
    record = m.record_from_dict(fixture("recipe"))
    with pytest.raises(ValidationError, match="passed checks"):
        replace(record, performance_outcome=m.Outcome.VALIDATED_IMPROVEMENT)
    with pytest.raises(ValidationError, match="cannot relax"):
        replace(record.compatibility, require_topology_match=False)


def test_user_explanation_distinguishes_intent_tools_and_observation():
    action = m.record_from_dict(fixture("trial")).actions[0]
    description = action.describe()
    for text in (
        "OMP_PROC_BIND",
        "required; planned",
        "OpenMP runtime",
        "child environment override",
        "Why:",
        "Expected effect:",
        "Requested:",
        "Resolved:",
        "Observed:",
        "not been observed",
        "hypothesis",
    ):
        assert text in description
    data = action.to_dict()
    data["observed_value"] = "spread"
    data["unavailable"] = {}
    with pytest.raises(ValidationError, match="evidence"):
        m.ActionRecord.from_dict(data)
    data["evidence"] = ["observations/runtime.json"]
    observed = m.ActionRecord.from_dict(data)
    assert observed.observed_value == "spread"


def test_toml_does_not_reinterpret_extension_payloads():
    text = (FIXTURES / "valid/workload.toml").read_text()
    text += '\n[extensions.vendor.unavailable]\nfield = "leave this opaque"\n'
    record = m.WorkloadSpec.from_toml(text)
    assert record.extensions == {"vendor": {"unavailable": {"field": "leave this opaque"}}}
    text = text.replace('version = "Not applicable in this synthetic example."', "")
    with pytest.raises(ValidationError, match="version"):
        m.WorkloadSpec.from_toml(text)


def test_every_nested_record_version_is_checked():
    for name, trail in [
        ("recipe", ["policy"]),
        ("trial", ["metrics", 0]),
        ("workload", ["cases", 0]),
    ]:
        data = fixture(name)
        nested = data
        for key in trail:
            nested = nested[key]
        nested["schema_version"] = 99
        with pytest.raises(ValidationError, match="schema_version"):
            m.record_from_dict(data)


def test_numbers_in_all_boundaries_reject_bool_nonfinite_and_huge_float_inputs():
    for name, trail in [
        ("resource", ["online_cpu_ids", 0]),
        ("policy", ["budget", "cpu_count"]),
        ("trial", ["order"]),
        ("experiment", ["seed"]),
        ("recipe", ["resource_constraints", "thread_count"]),
        ("metric", ["value"]),
        ("case", ["weight"]),
    ]:
        for value in (True, float("nan"), float("inf")):
            data = fixture(name)
            nested = data
            for key in trail[:-1]:
                nested = nested[key]
            nested[trail[-1]] = value
            with pytest.raises(ValidationError):
                m.record_from_dict(data)
    with pytest.raises(ValidationError):
        replace(m.record_from_dict(fixture("case")), weight=10**400)


def test_timeout_and_timestamps_cannot_report_success():
    data = fixture("trial")
    data.update(
        started_at="2026-09-16T12:00:00Z",
        ended_at="2026-09-16T12:00:01Z",
        return_code=0,
        timed_out=False,
        status="succeeded",
    )
    data["unavailable"] = {}
    assert m.record_from_dict(data).validation.state == m.ValidationState.UNCHECKED
    data["timed_out"] = True
    with pytest.raises(ValidationError):
        m.record_from_dict(data)
    data["status"] = "timed_out"
    assert m.record_from_dict(data).timed_out
    data["ended_at"] = "2026-09-16T11:00:00Z"
    with pytest.raises(ValidationError, match="end precedes"):
        m.record_from_dict(data)
    data["ended_at"] = "2026-09-16T13:00:00"
    with pytest.raises(ValidationError, match="timezone"):
        m.record_from_dict(data)


def test_output_hash_and_validator_command_shapes():
    fields = dict(
        id="check",
        output=None,
        sha256=None,
        json_field=None,
        expected=None,
        absolute_tolerance=None,
        relative_tolerance=None,
        argv=None,
        timeout_seconds=None,
        rationale="Synthetic scientific check",
    )
    for method, overrides in [
        ("file_hash", {"output": "result.txt", "sha256": "a" * 64}),
        ("command", {"argv": ["/synthetic/check", "α $INPUT"], "timeout_seconds": 2.0}),
    ]:
        data = {**fields, **overrides, "method": method}
        data["unavailable"] = {k: "Not applicable" for k, v in data.items() if v is None}
        assert m.OutputCheck.from_dict(data).method.value == method
        data["expected"] = 1.0
        del data["unavailable"]["expected"]
        with pytest.raises(ValidationError, match="match method"):
            m.OutputCheck.from_dict(data)


def test_documented_fields_match_the_frozen_python_contracts():
    from dataclasses import is_dataclass
    from enum import StrEnum

    text = (Path(__file__).resolve().parents[1] / "docs/formats.md").read_text()
    for name, cls in vars(m).items():
        if not isinstance(cls, type) or cls.__module__ != m.__name__:
            continue
        if issubclass(cls, StrEnum):
            assert f"`{name}`: " + ", ".join(f"`{item.value}`" for item in cls) + "." in text
        elif is_dataclass(cls) and name not in ("Model", "Record"):
            block = text.split(f"### {name}", 1)[1].split("### ", 1)[0]
            for key, annotation in cls.__annotations__.items():
                if key != "KIND":
                    escaped = annotation.replace("|", r"\|")
                    assert f"| `{key}` | `{escaped}` |" in block


def test_input_payloads_are_copied_and_applied_mechanisms_need_evidence():
    data = fixture("case")
    data["extensions"] = {"vendor": {"values": [1]}}
    record = m.record_from_dict(data)
    data["extensions"]["vendor"]["values"].append(2)
    assert record.extensions["vendor"]["values"] == [1]
    action = m.record_from_dict(fixture("trial")).actions[0]
    with pytest.raises(ValidationError, match="applied mechanism"):
        replace(action, status=m.ActionStatus.APPLIED)
    applied = replace(action, status=m.ActionStatus.APPLIED, evidence=["launch/environment.json"])
    assert "required; applied" in applied.describe()
    assert "not been observed" in applied.describe()


@pytest.mark.parametrize("reference", [True, 7, None, ["file.txt"]])
def test_artifact_helper_rejects_wrong_types(reference, tmp_path):
    with pytest.raises(ValidationError, match="string reference"):
        artifact_path(tmp_path, reference)


def test_records_cannot_contain_process_callbacks():
    data = fixture("policy")
    data["settings"][0]["value"] = lambda: None
    with pytest.raises(ValidationError, match="JSON data"):
        m.record_from_dict(data)
