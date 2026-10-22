"""Synthetic archive regressions; no personal identifiers or network access."""

import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest
from check_archives import (
    KIT,
    check_code_resources,
    check_identities,
    check_imports,
    check_links,
    check_privacy,
    private_findings,
    read_sdist,
    read_wheel,
    unpack_sdist,
    validate_sdist,
    validate_wheel,
)


@pytest.fixture
def payload():
    # Read resources from this isolated test kit. Installed runs contain no
    # simtune checkout: import tests below use explicit synthetic module bytes.
    root = Path(__file__).resolve().parents[1]
    policy = json.loads((root / "distribution/policy.json").read_text())
    files = {}
    for name in policy["files"]:
        if name.startswith("simtune/") or name in {
            "setup.py",
            "MANIFEST.in",
            ".github/workflows/ci.yml",
        }:
            continue
        entry = policy["curated"].get(name)
        source = root / (entry["source"] if entry else name)
        if not source.exists() and entry:
            source = root / name
        files[name] = source.read_bytes()
    # A selected fixture tree is enough for resource/link gates. Full artifact
    # selection/identity is exercised by the build audit, not an editable checkout.
    files["simtune/py.typed"] = b""
    return files


@pytest.mark.parametrize(
    "resource",
    sorted(
        {
            KIT + "native_gate.py",
            KIT + "record_command.py",
            KIT + "equivalent_launches.py",
            KIT + "protocol-pending.json",
            KIT + "evidence-status.json",
            "tests/process_helpers.py",
            "tests/fixtures/experiments/input.txt",
        }
    ),
)
def test_omitted_native_resource_fails_before_other_checks(payload, resource):
    del payload[resource]
    policy = {"files": list(payload) + [resource]}
    with pytest.raises(AssertionError, match="missing sdist files"):
        validate_sdist(payload, policy)


@pytest.mark.parametrize(
    "link",
    [
        "[input](missing.txt)",
        "[input][data]\n\n[data]: missing.txt",
        "[data][]\n\n[data]: missing.txt",
        "[data]\n\n[data]: missing.txt",
        "![image](missing.png)",
    ],
)
def test_missing_archive_link_never_uses_checkout(tmp_path, link):
    # Even a matching source file beside the audit must not resolve an archive link.
    (tmp_path / "missing.txt").write_text("checkout-only")
    with pytest.raises(AssertionError, match="broken local link"):
        check_links({"README.md": link.encode()})


def test_markdown_destinations_and_nonfilesystem_links():
    payload = {
        "docs/a.md": b'[x](<../file with space.txt>) [y](../file%20with%20space.txt#part)\n[z][ref]\n[ref]: ../nested(a).txt "title"\n[web](https://invalid.example/a) [mail](mailto:fixture@example.invalid) [part](#x)',
        "file with space.txt": b"",
        "nested(a).txt": b"",
    }
    assert check_links(payload) >= 3


def test_undefined_reference_fails():
    with pytest.raises(AssertionError, match="undefined Markdown reference"):
        check_links({"a.md": b"[missing][undefined]"})


@pytest.mark.parametrize(
    "private",
    [
        "/" + "home/" + "private-fixture-account/project",
        "/" + "Users/" + "private-fixture-account/project",
        "fixture-workstation" + ".local",
        '"hostname": "fixture-host-07"',
    ],
)
def test_private_details_in_public_document_fail(private):
    assert private_findings("docs/public.md", private)
    with pytest.raises(AssertionError, match="private account/host pattern"):
        check_privacy({"docs/public.md": private.encode()})


def test_portable_and_exact_synthetic_exceptions():
    assert not private_findings(
        "docs/public.md", "/SITE/project /home/USER/project /Users/<user>/project"
    )
    # Literal paths below are deliberately synthetic inputs, exempt only in this
    # exact test module. The same text in public prose must fail the heuristic.
    text = "/home/archive-fixture/project archive-fixture.local"
    assert not private_findings("tests/test_archive_audit.py", text)
    assert private_findings("docs/public.md", text)
    assert private_findings("tests/test_archive_audit.py", "/home/" + "another-fixture")


@pytest.mark.parametrize(
    "member",
    [
        "../escape",
        "/absolute",
        "root/../escape",
        "root/./file",
        "root\\escape",
        "C:/escape",
        "root//escape",
    ],
)
def test_unsafe_tar_paths_rejected(tmp_path, member):
    path = tmp_path / "unsafe.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo(member)
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(AssertionError, match="unsafe archive path"):
        read_sdist(path)
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_archive_links_and_special_files_rejected(tmp_path, kind):
    path = tmp_path / "unsafe.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo("root/file")
        info.type = kind
        info.linkname = "../../escape"
        tar.addfile(info)
    with pytest.raises(AssertionError, match="unsafe archive link/type"):
        read_sdist(path)


def test_duplicate_archive_member_rejected(tmp_path):
    path = tmp_path / "duplicate.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for _ in range(2):
            tar.addfile(tarfile.TarInfo("root/file"), io.BytesIO(b""))
    with pytest.raises(AssertionError, match="duplicate archive member"):
        read_sdist(path)


def test_wheel_symlink_rejected(tmp_path):
    path = tmp_path / "link.whl"
    with zipfile.ZipFile(path, "w") as wheel:
        info = zipfile.ZipInfo("simtune/link.py")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        wheel.writestr(info, "../escape")
    with pytest.raises(AssertionError, match="unsafe wheel link"):
        read_wheel(path)


def test_transitive_local_import_missing():
    with pytest.raises(AssertionError, match="missing import dependency"):
        check_imports(
            {
                KIT + "native_gate.py": b"from process_helpers import cleanup",
                "tests/process_helpers.py": b"from test_checks import command_check",
            }
        )
    check_imports(
        {
            KIT + "native_gate.py": b"from process_helpers import cleanup",
            "tests/process_helpers.py": b"from test_checks import command_check",
            "tests/test_checks.py": b"import pytest\nimport json",
        }
    )


def test_scientific_dependencies_are_scoped_to_the_optional_example():
    check_imports({"examples/digits_workload.py": b"import numpy, sklearn, threadpoolctl"})
    with pytest.raises(AssertionError, match="missing import dependency"):
        check_imports({"simtune/example.py": b"import numpy"})


def test_code_block_input_missing(payload):
    payload["docs/input.md"] = b"```python\npath = KIT / 'examples/" + b"absent_input.py'\n```"
    with pytest.raises(AssertionError, match="missing code resource"):
        check_code_resources(payload)


def test_private_record_inclusion_fails(payload):
    name = "docs/implementation/release-plan/handoffs/private.md"
    payload[name] = b"private historical record"
    with pytest.raises(AssertionError, match="private/build path"):
        validate_sdist(payload, {"files": list(payload)})


def test_public_link_graph(payload):
    assert {".gitignore", "docs/user-guide.md", "docs/candidate-review.md"} <= payload.keys()
    assert check_links(payload) > 100
    check_code_resources(payload)


def test_exact_identity_for_production_and_curated_source(tmp_path):
    import hashlib

    code = tmp_path / "package.py"
    code.write_bytes(b"production bytes")
    public = tmp_path / "public.txt"
    public.write_bytes(b"portable public bytes")
    (tmp_path / "original.txt").write_bytes(b"private original bytes")
    policy = {
        "files": ["package.py", "original.txt"],
        "curated": {
            "original.txt": {
                "source": "public.txt",
                "sha256": hashlib.sha256(public.read_bytes()).hexdigest(),
            }
        },
    }
    payload = {"package.py": code.read_bytes(), "original.txt": public.read_bytes()}
    check_identities(payload, tmp_path, policy)
    payload["package.py"] = b"changed production"
    with pytest.raises(AssertionError, match="sdist source identity"):
        check_identities(payload, tmp_path, policy)
    payload["package.py"] = code.read_bytes()
    public.write_bytes(b"changed curated source")
    with pytest.raises(AssertionError, match="curated identity"):
        check_identities(payload, tmp_path, policy)


def test_wheel_extra_and_changed_package_rejected():
    package = {"simtune/module.py": b"package bytes", "simtune/py.typed": b""}
    wheel = {**package, "simtune-0.1.0.dist-info/licenses/LICENSE": b"license"}
    validate_wheel(wheel, package, b"license")
    wheel["private/document.md"] = b"private"
    with pytest.raises(AssertionError, match="non-package wheel file"):
        validate_wheel(wheel, package, b"license")
    del wheel["private/document.md"]
    wheel["simtune/module.py"] = b"changed"
    with pytest.raises(AssertionError, match="wheel source identity"):
        validate_wheel(wheel, package, b"license")


def test_removed_resource_from_actual_tar(payload, tmp_path):
    path = tmp_path / "missing.tar.gz"
    policy = {"files": list(payload)}
    with tarfile.open(path, "w:gz") as tar:
        for name, data in payload.items():
            if name == KIT + "record_command.py":
                continue
            info = tarfile.TarInfo("root/" + name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    with pytest.raises(AssertionError, match="missing sdist files"):
        validate_sdist(read_sdist(path), policy)


def test_unpack_preserves_executable_and_rejects_before_writes(tmp_path):
    path = tmp_path / "executable.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo("root/fixture.py")
        info.mode = 0o755
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    destination = tmp_path / "unpacked"
    unpack_sdist(path, destination)
    assert (destination / "fixture.py").stat().st_mode & 0o777 == 0o755
    with tarfile.open(path, "w:gz") as tar:
        tar.addfile(tarfile.TarInfo("root/safe"), io.BytesIO(b""))
        tar.addfile(tarfile.TarInfo("root/../escape"), io.BytesIO(b""))
    refused = tmp_path / "refused"
    with pytest.raises(AssertionError, match="unsafe archive path"):
        unpack_sdist(path, refused)
    assert not refused.exists()


def test_special_permissions_rejected(tmp_path):
    path = tmp_path / "setuid.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo("root/script")
        info.mode = 0o4755
        tar.addfile(info, io.BytesIO(b""))
    with pytest.raises(AssertionError, match="unsafe archive permissions"):
        read_sdist(path)


@pytest.mark.parametrize(
    "field,value", [("uid", 501), ("gid", 20), ("uname", "builder"), ("gname", "staff")]
)
@pytest.mark.parametrize("directory", [False, True])
def test_archive_ownership_is_neutral_for_every_member(tmp_path, field, value, directory):
    path = tmp_path / "owner.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo("root/directory" if directory else "root/file")
        if directory:
            info.type = tarfile.DIRTYPE
        setattr(info, field, value)
        tar.addfile(info, io.BytesIO(b""))
    with pytest.raises(AssertionError, match="non-neutral archive ownership"):
        read_sdist(path)


@pytest.mark.parametrize("key", ["SCHILY.xattr.user.creator", "comment", "atime"])
def test_archive_extended_metadata_is_rejected(tmp_path, key):
    path = tmp_path / "metadata.tar.gz"
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo("root/file")
        info.pax_headers[key] = "private build metadata"
        tar.addfile(info, io.BytesIO(b""))
    with pytest.raises(AssertionError, match="unexpected archive metadata"):
        read_sdist(path)


def test_file_uri_cannot_hide_checkout_dependency():
    with pytest.raises(AssertionError, match="unsupported filesystem link URI"):
        check_links({"a.md": b"[local](file:///unavailable-checkout.txt)"})


@pytest.mark.parametrize(
    "target",
    [
        "file://localhost/tmp/missing.md",
        "file://private-host/path/missing.md",
        "ssh://private-host/path/missing.md",
        "file://localhost",
        "file://private-host",
        "ssh://private-host",
        "file://private-host#part",
        "ssh:",
        "file:",
    ],
)
def test_unsupported_scheme_rejected_before_authority_or_empty_path(target):
    with pytest.raises(AssertionError, match="unsupported filesystem link URI"):
        check_links({"a.md": f"[resource]({target})".encode()})


@pytest.mark.parametrize(
    "target",
    [
        "https://invalid.example/a",
        "https://invalid.example",
        "http://invalid.example",
        "mailto:fixture@example.invalid",
        "//invalid.example/a",
        "//invalid.example",
        "#part",
    ],
)
def test_supported_web_mail_and_fragment_links_need_no_archive_resource(target):
    assert check_links({"a.md": f"[resource]({target})".encode()}) == 0
