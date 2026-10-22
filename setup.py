"""Select the public sdist without rewriting private checkout records."""

import gzip
import hashlib
import json
import os
import shutil
import tarfile
from pathlib import Path

from setuptools import setup
from setuptools.command.sdist import sdist


class PublicSdist(sdist):
    def make_archive(self, base_name, format, root_dir=None, base_dir=None, owner=None, group=None):
        """Emit portable headers without leaking build-account or filesystem metadata."""
        if format != "gztar":
            raise ValueError("public source distributions require gztar format")
        epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
        if not 0 <= epoch <= 0xFFFFFFFF:
            raise ValueError("SOURCE_DATE_EPOCH must fit an unsigned 32-bit timestamp")
        archive_name = base_name + ".tar.gz"
        if self.dry_run:
            return archive_name
        source = Path(root_dir or ".") / (base_dir or ".")

        def portable(info):
            if not (info.isfile() or info.isdir()):
                raise ValueError(f"unsafe source distribution member: {info.name}")
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = epoch
            info.pax_headers = {}
            info.mode = 0o755 if info.isdir() or info.mode & 0o111 else 0o644
            return info

        Path(archive_name).parent.mkdir(parents=True, exist_ok=True)
        with open(archive_name, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as tar:
                    tar.add(source, arcname=base_dir or source.name, filter=portable)
        return archive_name

    def make_release_tree(self, base_dir, files):
        root = Path.cwd()
        policy = json.loads((root / "distribution/policy.json").read_text())
        selected = set(policy["files"])
        for name in selected | {entry["source"] for entry in policy["curated"].values()}:
            path = Path(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name:
                raise ValueError("unsafe public policy path")
        curated = policy["curated"]
        super().make_release_tree(base_dir, files)
        release = Path(base_dir)
        # Setuptools may discover defaults or stale egg-info entries. Nothing
        # outside the explicit policy and generated metadata may enter the tar.
        for path in release.rglob("*"):
            if path.is_symlink():
                path.unlink()
                continue
            if not path.is_file():
                continue
            name = path.relative_to(release).as_posix()
            generated = name in {"PKG-INFO", "setup.cfg"} | {
                "simtune.egg-info/" + item
                for item in (
                    "PKG-INFO",
                    "SOURCES.txt",
                    "dependency_links.txt",
                    "entry_points.txt",
                    "requires.txt",
                    "top_level.txt",
                )
            }
            if name not in selected and not generated:
                path.unlink()
        for name in sorted(selected):
            entry = curated.get(name)
            source = root / (entry["source"] if entry else name)
            if entry and not source.exists():
                # A rebuilt unpacked sdist already contains the curated bytes at
                # their public destination. Never fall back to private records.
                source = root / name
            if not source.resolve().is_relative_to(root.resolve()):
                raise ValueError(f"escaping public source: {name}")
            if source.is_symlink() or not source.is_file():
                raise ValueError(f"missing/unsafe public source: {name}")
            if entry and hashlib.sha256(source.read_bytes()).hexdigest() != entry["sha256"]:
                raise ValueError(f"curated source identity mismatch: {name}")
            target = release / name
            target.parent.mkdir(parents=True, exist_ok=True)
            # Break any hardlink created by setuptools before replacing bytes.
            if target.exists():
                target.unlink()
            shutil.copy2(source, target)
        for path in sorted(release.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        sources = release / "simtune.egg-info/SOURCES.txt"
        sources.write_text(
            "\n".join(
                sorted(p.relative_to(release).as_posix() for p in release.rglob("*") if p.is_file())
            )
            + "\n"
        )


setup(cmdclass={"sdist": PublicSdist})
