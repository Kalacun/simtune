"""Stage-04 workload identity and substitution tests."""

import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from simtune.validation import ValidationError
from simtune.workloads import expand_trial_argv, load_case, load_workload, sha256_file

FIXTURE = Path(__file__).parent / "fixtures" / "experiments" / "workload.toml"


def test_load_workload_resolves_from_manifest_not_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    loaded = load_workload(FIXTURE)
    root = FIXTURE.parent.resolve()
    assert loaded.spec.base_directory == str(root)
    assert loaded.spec.resolved_executable == str(root / "workload.py")
    assert dict(loaded.artifact_paths)["case:tiny:tiny-input"] == root / "input.txt"
    assert dict(loaded.relevant_environment) == {"SCIENCE_SEED": "7"}
    assert len(loaded.workload_fingerprint) == 64
    assert loaded.untracked_external_dependencies


def test_changed_executable_and_input_hashes_are_rejected(tmp_path):
    fixture = tmp_path / "fixture"
    shutil.copytree(FIXTURE.parent, fixture)
    executable = fixture / "workload.py"
    executable.chmod(0o755)
    executable.write_text(executable.read_text() + "\n# changed\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="executable identity changed"):
        load_workload(fixture / "workload.toml")

    shutil.rmtree(fixture)
    shutil.copytree(FIXTURE.parent, fixture)
    (fixture / "workload.py").chmod(0o755)
    (fixture / "input.txt").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="SHA-256 changed"):
        load_workload(fixture / "workload.toml")


def test_literal_substitution_is_whole_argument_and_not_recursive(tmp_path):
    loaded = load_workload(FIXTURE)
    case = replace(loaded.spec.cases[0], arguments={"input": "{trial_dir}"})
    argv = expand_trial_argv(loaded.spec, case, tmp_path)
    assert argv[1] == "{trial_dir}"
    assert argv[2] == str(tmp_path)
    assert (
        "$HOME"
        == expand_trial_argv(
            replace(loaded.spec, argv_template=[loaded.spec.resolved_executable, "$HOME"]),
            case,
            tmp_path,
        )[1]
    )


def test_standalone_case_loading_checks_declared_files(tmp_path):
    loaded = load_workload(FIXTURE)
    case_path = tmp_path / "case.json"
    case_path.write_text(loaded.spec.cases[0].to_json(), encoding="utf-8")
    case = load_case(case_path, base_directory=loaded.spec.base_directory)
    assert case.id == "tiny"


def test_manifest_type_and_executable_mismatch_are_clear(tmp_path):
    text = FIXTURE.read_text(encoding="utf-8")
    bad = tmp_path / "workload.toml"
    bad.write_text(text.replace('argv_template = ["workload.py"', 'argv_template = ["input.txt"'))
    shutil.copy(FIXTURE.parent / "workload.py", tmp_path / "workload.py")
    shutil.copy(FIXTURE.parent / "input.txt", tmp_path / "input.txt")
    (tmp_path / "workload.py").chmod(0o755)
    with pytest.raises(ValidationError, match="argv_template"):
        load_workload(bad)
    unknown = tmp_path / "workload.yaml"
    unknown.write_text("ignored", encoding="utf-8")
    with pytest.raises(ValidationError, match=".toml or .json"):
        load_workload(unknown)


def test_streaming_hash_matches_declared_identity():
    loaded = load_workload(FIXTURE)
    assert sha256_file(loaded.spec.resolved_executable) == loaded.spec.executable_sha256
    assert os.access(loaded.spec.resolved_executable, os.X_OK)


def test_json_exchange_manifest_loads_to_same_identity(tmp_path):
    loaded = load_workload(FIXTURE)
    exchange = tmp_path / "workload.json"
    exchange.write_text(loaded.spec.to_json(), encoding="utf-8")
    reloaded = load_workload(exchange)
    assert reloaded.spec == loaded.spec
    assert reloaded.workload_fingerprint == loaded.workload_fingerprint
