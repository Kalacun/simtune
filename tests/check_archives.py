"""Deterministic developer audit of actual archives, never checkout link fallback.

Content scanning is a heuristic plus explicit selection, not a privacy proof.
"""

import ast
import hashlib
import json
import posixpath
import re
import stat
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

KIT = "docs/implementation/release-plan/r06/"
PUBLIC_POLICY = "distribution/policy.json"
TEXT_SUFFIXES = {
    ".md",
    ".py",
    ".json",
    ".toml",
    ".yaml",
    ".txt",
    ".c",
    ".sh",
    ".sbatch",
    ".in",
    ".yml",
}
# These are reviewed literal synthetic regression inputs, not whole-file exemptions.
SYNTHETIC_PRIVATE_EXCEPTIONS = {
    ("tests/test_archive_audit.py", "/home/" + "archive-fixture"),
    ("tests/test_archive_audit.py", "archive-fixture" + ".local"),
    ("tests/test_archive_audit.py", "fixture-host-07"),
}
PORTABLE_ACCOUNTS = {"USER", "USERNAME", "ACCOUNT", "<user>", "<account>"}
PRIVATE_PATTERNS = (
    re.compile(r"/(?:Users|home)/([A-Za-z0-9_.-]+|<[^>]+>)"),
    re.compile(r"\b[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)*\.(?:local|internal)\b"),
    re.compile(r"""(?:hostname|host_name)["']?\s*[:=]\s*["']([^"'\n]+)["']"""),
)
# Mandatory workflow inputs, including transitive test imports/fixture artifacts.
NATIVE_RESOURCES = {
    KIT + name
    for name in (
        "RUNBOOK.md",
        "native_gate.py",
        "record_command.py",
        "equivalent_launches.py",
        "protocol-pending.json",
        "evidence-status.json",
        "report-pending.md",
    )
} | {
    "tests/process_helpers.py",
    "tests/test_checks.py",
    "tests/test_search.py",
    "tests/test_runbook_taskset.py",
    "tests/fixtures/experiments/workload.toml",
    "tests/fixtures/experiments/workload.py",
    "tests/fixtures/experiments/input.txt",
    "examples/heat2d.c",
    "examples/make_heat_workload.py",
    "examples/check_heat_result.py",
    "examples/heat_benchmark.py",
    "examples/HEAT_EXPERIMENT.md",
    "examples/heat-smoke.toml",
    "profiles/heat-experiment.yaml",
    "profiles/default.yaml",
    "simtune/py.typed",
}


def safe_name(name):
    parts = PurePosixPath(name).parts
    assert name and not name.startswith("/") and "\\" not in name, f"unsafe archive path: {name}"
    assert not re.match(r"^[A-Za-z]:", name), f"unsafe archive path: {name}"
    assert not {"..", ".", ""} & set(name.split("/")), f"unsafe archive path: {name}"
    assert parts, f"unsafe archive path: {name}"
    return name


def read_sdist(path):
    payload = {}
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        assert members, "empty sdist"
        prefix = safe_name(members[0].name).split("/")[0]
        for member in members:
            safe_name(member.name.rstrip("/") if member.isdir() else member.name)
            assert (member.uid, member.gid, member.uname, member.gname) == (0, 0, "", ""), (
                f"non-neutral archive ownership: {member.name}"
            )
            assert set(member.pax_headers) <= {"path", "mtime", "size"}, (
                f"unexpected archive metadata: {member.name}"
            )
            assert not member.mode & 0o7000, f"unsafe archive permissions: {member.name}"
            assert member.name.split("/")[0] == prefix, "multiple sdist roots"
            assert member.isdir() or member.isfile(), f"unsafe archive link/type: {member.name}"
            if member.isfile():
                assert "/" in member.name, "file outside sdist root"
                name = safe_name(member.name.split("/", 1)[1])
                assert name not in payload, f"duplicate archive member: {name}"
                payload[name] = archive.extractfile(member).read()
    return payload


def unpack_sdist(path, directory):
    """Validate every member before creating a fresh unpacking; preserve safe modes."""
    payload = read_sdist(path)
    with tarfile.open(path) as archive:
        modes = {
            member.name.split("/", 1)[1]: member.mode
            for member in archive.getmembers()
            if member.isfile()
        }
    directory.mkdir(parents=True, exist_ok=False)
    for name, data in payload.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(modes[name])
    return payload


def read_wheel(path):
    payload = {}
    with zipfile.ZipFile(path) as archive:
        for entry in archive.infolist():
            safe_name(entry.filename.rstrip("/"))
            assert not stat.S_ISLNK(entry.external_attr >> 16), (
                f"unsafe wheel link: {entry.filename}"
            )
            if not entry.is_dir():
                assert entry.filename not in payload, f"duplicate wheel member: {entry.filename}"
                payload[entry.filename] = archive.read(entry)
    return payload


def markdown_targets(text):
    """Inline/images and full/collapsed/shortcut reference links, outside fences.

    Balanced destination parentheses and angle-wrapped paths are supported. No
    network or fragment existence test; fragments are not filesystem resources.
    """
    text = re.sub(r"^```[^\n]*\n.*?^```[^\n]*$", "", text, flags=re.M | re.S)
    # Inline code can contain bracket syntax such as character classes; it is
    # not a Markdown reference. Keep link labels parseable with a neutral token.
    text = re.sub(r"(`+)(.*?)\1", "CODE", text, flags=re.S)
    definitions = {}
    for match in re.finditer(r"^\s{0,3}\[([^\]]+)\]:\s*(<[^>]+>|\S+)", text, re.M):
        definitions[" ".join(match[1].lower().split())] = match[2].strip("<>")
    targets = list(definitions.values())
    # Parse the destination only, respecting nested parentheses and optional title.
    for match in re.finditer(r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\(", text):
        tail = text[match.end() :].lstrip()
        if tail.startswith("<"):
            end = tail.find(">")
            assert end >= 0, "unterminated Markdown destination"
            targets.append(tail[1:end])
            continue
        depth = 0
        destination = []
        for char in tail:
            if char == ")" and depth == 0 or char.isspace() and depth == 0:
                break
            depth += (char == "(") - (char == ")")
            destination.append(char)
        targets.append("".join(destination))
    for match in re.finditer(r"(?<!!)\[([^\[\]\n]+)\](?:\[([^\]\n]*)\])?", text):
        if text[match.end() : match.end() + 1] in {"(", ":"}:
            continue
        label = match[2] or match[1]
        key = " ".join(label.lower().split())
        if key in definitions:
            targets.append(definitions[key])
        elif match[2] is not None:
            raise AssertionError(f"undefined Markdown reference: {label}")
    return targets


def check_links(payload):
    checked = 0
    for name, data in payload.items():
        if not name.endswith(".md"):
            continue
        for target in markdown_targets(data.decode()):
            url = urlsplit(target)
            if url.scheme in {"http", "https", "mailto"}:
                continue
            assert not url.scheme, f"unsupported filesystem link URI: {name} -> {target}"
            if url.netloc or not url.path:
                continue
            path = unquote(url.path)
            assert not path.startswith("/"), f"absolute local link: {name} -> {target}"
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), path))
            assert resolved != ".." and not resolved.startswith("../"), f"escaping link: {name}"
            exists = resolved in payload or any(
                p.startswith(resolved.rstrip("/") + "/") for p in payload
            )
            assert exists, f"broken local link: {name} -> {target}"
            checked += 1
    return checked


def private_findings(name, text):
    findings = []
    for index, pattern in enumerate(PRIVATE_PATTERNS):
        for match in pattern.finditer(text):
            value = match[0] if index < 2 else match[1]
            if index == 0 and match[1] in PORTABLE_ACCOUNTS:
                continue
            if (name, value) in SYNTHETIC_PRIVATE_EXCEPTIONS:
                continue
            findings.append(value)
    return findings


def check_privacy(payload):
    for name, data in payload.items():
        if PurePosixPath(name).suffix in TEXT_SUFFIXES or name.endswith(("PKG-INFO", "METADATA")):
            # Do not echo sensitive excerpts in public audit output.
            assert not private_findings(name, data.decode()), (
                f"private account/host pattern: {name}"
            )


def check_imports(payload):
    """Resolve local module imports using archive members only; optional tools are declared."""
    external = sys.stdlib_module_names | {"pytest", "yaml", "rich", "setuptools"}
    example_dependencies = {
        "examples/digits_workload.py": {"numpy", "sklearn", "threadpoolctl"},
    }
    trees = {}
    for name, data in payload.items():
        if name.endswith(".py"):
            trees[name] = ast.parse(data.decode(), filename=name)
    for name, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            modules = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for module in modules:
                top = module.split(".")[0]
                if not isinstance(node, ast.ImportFrom) or not node.level:
                    if top in external or top in example_dependencies.get(name, set()):
                        continue
                stem = module.replace(".", "/")
                if isinstance(node, ast.ImportFrom) and node.level:
                    base = PurePosixPath(name).parent
                    for _ in range(node.level - 1):
                        base = base.parent
                    stems = [str(base / stem)]
                else:
                    stems = [stem, "tests/" + stem, posixpath.dirname(name) + "/" + stem]
                assert any(
                    s + ".py" in payload
                    or s + "/__init__.py" in payload
                    or any(p.startswith(s + "/") for p in payload)
                    for s in stems
                ), f"missing import dependency: {name} -> {module}"
    return trees


def check_code_resources(payload):
    """Check literal repository inputs in prose/code, including non-link prerequisites.

    Generated SESSION files and explicitly operator-authored site inputs are not
    source resources. KIT/existing repository-root paths are never excused that way.
    """
    for name, data in payload.items():
        # Synthetic test strings describe missing/output paths as test data.
        # Their real local imports are checked separately across the entire tree.
        if name.startswith("tests/") and name.endswith(".py"):
            continue
        if not name.endswith((".py", ".md", ".toml", ".sh", ".sbatch")):
            continue
        text = data.decode()
        # A broad stem (examples/, tests/, profiles/, KIT/) must resolve inside
        # the payload even when it appears only in a command or a code block.
        targets = re.findall(
            r"""(?:examples|tests|profiles|docs)/[A-Za-z0-9_./-]+\.(?:py|c|toml|yaml|json|md|sbatch|sh|txt)""",
            text,
        )
        targets += re.findall(
            r"""KIT\s*/\s*["']([^"']+\.(?:py|c|toml|yaml|json|md|txt))["']""", text
        )
        for target in targets:
            assert target in payload, f"missing code resource: {name} -> {target}"
    runbook = payload[KIT + "RUNBOOK.md"].decode()
    for block in re.findall(r"^```python\n(.*?)^```", runbook, re.M | re.S):
        tree = ast.parse(block)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in {"record_command", "r06_site"}:
                if node.module == "r06_site":
                    assert "Save as r06_site.py" in runbook, (
                        "site input lacks creation instructions"
                    )
                else:
                    assert KIT + node.module + ".py" in payload, "missing runbook import"
            # Focused test list entries have no directory prefix in this runbook.
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and re.fullmatch(r"test_\w+\.py", node.value)
            ):
                assert "tests/" + node.value in payload, f"missing runbook test: {node.value}"
    assert runbook.count("def restricted(argv):") == 1, (
        "one authoritative restricted function required"
    )


def validate_sdist(payload, policy):
    required = set(policy["files"]) | NATIVE_RESOURCES
    assert required <= payload.keys(), f"missing sdist files: {sorted(required - payload.keys())}"
    metadata = {"PKG-INFO", "setup.cfg"} | {
        "simtune.egg-info/" + name
        for name in (
            "PKG-INFO",
            "SOURCES.txt",
            "dependency_links.txt",
            "entry_points.txt",
            "requires.txt",
            "top_level.txt",
        )
    }
    assert payload.keys() <= required | metadata, (
        f"unselected public payload: {sorted(payload.keys() - required - metadata)}"
    )
    for name in payload:
        safe_name(name)
        assert not {
            "__pycache__",
            ".venv",
            "runs",
            "reports",
            "zagovor",
            "benchmark-results",
            "handoffs",
            "build",
            "dist",
            "curated",
        } & set(PurePosixPath(name).parts), f"private/build path: {name}"
        assert not name.endswith((".pyc", ".so", ".dylib", ".o")), f"build product: {name}"
    check_privacy(payload)
    links = check_links(payload)
    check_imports(payload)
    check_code_resources(payload)
    protocol = json.loads(payload[KIT + "protocol-pending.json"])
    status = json.loads(payload[KIT + "evidence-status.json"])
    assert protocol["state"] == "prepared-not-frozen" and protocol["target"] is None
    assert status["native_functional_gate"] == "not run" and status["native_sample_dataset"] is None
    return links


def check_identities(payload, root, policy):
    for name in policy["files"]:
        entry = policy["curated"].get(name)
        source = root / (entry["source"] if entry else name)
        if entry and not source.exists():
            source = root / name  # Only the identified, hash-checked unpacked counterpart.
        expected = source.read_bytes()
        if entry:
            assert hashlib.sha256(expected).hexdigest() == entry["sha256"], (
                f"curated identity: {name}"
            )
        assert payload[name] == expected, f"sdist source identity: {name}"


def validate_wheel(wheel, package_payload, license_bytes):
    assert package_payload.keys() <= wheel.keys(), "missing wheel package file"
    metadata_root = "simtune-0.1.0.dist-info"
    metadata_names = {
        metadata_root + "/" + name
        for name in (
            "METADATA",
            "WHEEL",
            "entry_points.txt",
            "top_level.txt",
            "RECORD",
            "licenses/LICENSE",
        )
    }
    assert wheel.keys() <= package_payload.keys() | metadata_names, "non-package wheel file"
    for name, data in package_payload.items():
        assert wheel[name] == data, f"wheel source identity: {name}"
    assert wheel[metadata_root + "/licenses/LICENSE"] == license_bytes
    check_privacy(wheel)


def audit(directory, root):
    wheels = list(directory.glob("*.whl"))
    sdists = list(directory.glob("*.tar.gz"))
    assert len(wheels) == len(sdists) == 1, "expect one wheel and one sdist"
    payload = read_sdist(sdists[0])
    policy = json.loads((root / PUBLIC_POLICY).read_text())
    assert payload[PUBLIC_POLICY] == (root / PUBLIC_POLICY).read_bytes(), "policy source mismatch"
    links = validate_sdist(payload, policy)
    check_identities(payload, root, policy)
    with tarfile.open(sdists[0]) as archive:
        modes = {m.name.split("/", 1)[1]: m.mode for m in archive.getmembers() if m.isfile()}
    for name in policy["files"]:
        entry = policy["curated"].get(name)
        source = root / (entry["source"] if entry else name)
        if entry and not source.exists():
            source = root / name
        assert modes[name] == stat.S_IMODE(source.stat().st_mode), f"sdist source mode: {name}"
    wheel = read_wheel(wheels[0])
    package_payload = {
        name: payload[name] for name in policy["files"] if name.startswith("simtune/")
    }
    validate_wheel(wheel, package_payload, (root / "LICENSE").read_bytes())
    print(
        f"Archives match identified sources; {links} local links resolve; native resources/imports, selection/privacy scan and package-only wheel passed"
    )
    for path in (wheels[0], sdists[0]):
        print(path.name, hashlib.sha256(path.read_bytes()).hexdigest())


if __name__ == "__main__":
    audit(Path(sys.argv[1]), Path(sys.argv[2]).resolve())
