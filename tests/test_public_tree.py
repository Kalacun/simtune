import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from distribution.prepare_public import build_public_tree

ROOT = Path(__file__).resolve().parents[1]
# The installed-package kit deliberately lacks package sources. Exercise source
# preparation against the separately unpacked audited sdist, without adding it
# to Python's import path or weakening the installed-import assertion.
SOURCE_ROOT = Path(os.environ.get("SIMTUNE_TEST_SOURCE_ROOT", ROOT))
SCRIPT = SOURCE_ROOT / "distribution/prepare_public.py"


def test_public_tree_contains_exact_selected_content(tmp_path):
    destination = tmp_path / "public"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(destination)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    expected = set(json.loads((SOURCE_ROOT / "distribution/policy.json").read_text())["files"])
    actual = {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    }
    assert actual == expected
    assert {item["path"] for item in report["files"]} == expected
    assert "docs/implementation/release-plan/handoffs/R06-linux-evidence.md" not in actual
    assert (destination / ".gitignore").is_file()


def test_public_tree_is_outside_source_and_never_overwrites(tmp_path):
    destination = tmp_path / "existing"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("keep")
    with pytest.raises(FileExistsError):
        build_public_tree(ROOT, destination)
    assert sentinel.read_text() == "keep"
    with pytest.raises(ValueError, match="separate"):
        build_public_tree(ROOT, ROOT / "nested-public")


@pytest.mark.parametrize("name", ["../secret", "/absolute", "a\\b", "file:secret"])
def test_public_tree_rejects_unsafe_policy_names(tmp_path, name):
    source = tmp_path / "source"
    (source / "distribution").mkdir(parents=True)
    (source / "distribution/policy.json").write_text(json.dumps({"files": [name], "curated": {}}))
    (source / "secret").write_text("private")
    with pytest.raises(ValueError, match="unsafe public policy path"):
        build_public_tree(source, tmp_path / "output")


def test_public_tree_checks_curated_hash_and_rejects_source_symlink(tmp_path):
    source = tmp_path / "source"
    (source / "distribution").mkdir(parents=True)
    (source / "private.md").write_text("private")
    (source / "public.md").write_text("public")
    policy = {
        "files": ["public.md"],
        "curated": {"public.md": {"source": "private.md", "sha256": "0" * 64}},
    }
    (source / "distribution/policy.json").write_text(json.dumps(policy))
    with pytest.raises(ValueError, match="hash mismatch"):
        build_public_tree(source, tmp_path / "hash-output")

    (source / "distribution/policy.json").write_text(json.dumps({"files": ["public.md"]}))
    (source / "public.md").unlink()
    (source / "public.md").symlink_to(source / "private.md")
    with pytest.raises(ValueError, match="symlink"):
        build_public_tree(source, tmp_path / "link-output")


def test_public_tree_can_rebuild_curated_public_destinations(tmp_path):
    source = tmp_path / "source"
    (source / "distribution").mkdir(parents=True)
    (source / "public.md").write_bytes(b"portable public bytes")
    policy = {
        "files": ["public.md"],
        "curated": {
            "public.md": {
                "source": "curated/public.md",
                "sha256": hashlib.sha256(b"portable public bytes").hexdigest(),
            }
        },
    }
    (source / "distribution/policy.json").write_text(json.dumps(policy))
    build_public_tree(source, tmp_path / "public")
    assert (tmp_path / "public/public.md").read_bytes() == b"portable public bytes"
    (source / "public.md").write_bytes(b"wrong replacement")
    with pytest.raises(ValueError, match="hash mismatch"):
        build_public_tree(source, tmp_path / "refused")
