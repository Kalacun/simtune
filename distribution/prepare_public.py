"""Build a fresh GitHub source tree from the explicit public file policy.

This creates selected source content. It does not certify archive metadata,
native support, test results, privacy beyond the allowlist, or release readiness.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


def _safe_relative(value: str) -> PurePosixPath:
    if not isinstance(value, str):
        raise ValueError(f"unsafe public policy path: {value!r}")
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "\\" in value
        or ":" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError(f"unsafe public policy path: {value!r}")
    return path


def _read_source(root: Path, name: str) -> bytes:
    relative = _safe_relative(name)
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"public policy source is a symlink: {name}")
    if not path.exists():
        raise FileNotFoundError(f"missing public policy source: {name}")
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise ValueError(f"missing or escaping public policy source: {name}")
    return path.read_bytes()


def build_public_tree(source_root: Path, destination: Path) -> dict[str, Any]:
    """Create `destination` atomically from policy-selected files only."""
    root = source_root.resolve(strict=True)
    requested = destination.expanduser().absolute()
    if requested.is_symlink():
        raise FileExistsError(f"destination is a symlink: {requested}")
    target = requested.resolve()
    if target == root or root in target.parents or target in root.parents:
        raise ValueError("public tree destination and source must be separate")
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"destination already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    policy_data = json.loads(_read_source(root, "distribution/policy.json"))
    names = policy_data.get("files")
    curated = policy_data.get("curated", {})
    if (
        not isinstance(names, list)
        or not names
        or any(not isinstance(name, str) for name in names)
        or len(names) != len(set(names))
    ):
        raise ValueError("distribution policy files must be a non-empty unique list")
    if not isinstance(curated, dict) or not set(curated) <= set(names):
        raise ValueError("distribution policy has invalid curated destinations")

    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.stage-", dir=target.parent))
    inventory = []
    try:
        for name in sorted(names):
            _safe_relative(name)
            entry = curated.get(name)
            if entry is not None and not isinstance(entry, dict):
                raise ValueError(f"invalid curated mapping for {name}")
            source_name = entry.get("source", name) if entry else name
            # Public trees and unpacked sdists contain only the curated destination.
            # Fall back only for a missing source, never an unsafe path or symlink.
            try:
                data = _read_source(root, source_name)
            except FileNotFoundError:
                if not entry:
                    raise
                source_name = name
                data = _read_source(root, source_name)
            digest = hashlib.sha256(data).hexdigest()
            expected = entry.get("sha256") if entry else None
            if expected is not None and digest != expected:
                raise ValueError(f"curated source hash mismatch: {name}")
            destination_path = staging / Path(*_safe_relative(name).parts)
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            destination_path.write_bytes(data)
            original = root / Path(*_safe_relative(source_name).parts)
            destination_path.chmod(original.stat().st_mode & 0o777)
            inventory.append({"path": name, "sha256": digest})
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"destination": str(target), "files": inventory}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="new directory for the selected public tree")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        result = build_public_tree(root, args.destination)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"prepare_public: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
