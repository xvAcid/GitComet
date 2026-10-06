#!/usr/bin/env python3
"""Package the selected source revision and its matching Linux GUI binary."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib


SOURCE_ROOT = Path(__file__).resolve().parents[2]
TARGET = "x86_64-unknown-linux-gnu"


def package(source_root, binary, output_dir):
    source_root = Path(source_root).resolve()
    binary = Path(binary).resolve()
    output_dir = Path(output_dir).resolve()
    status = subprocess.run(["git", "-C", str(source_root), "diff", "--quiet", "HEAD", "--"])
    if status.returncode == 1:
        raise ValueError("Commit tracked source changes before packaging")
    status.check_returncode()
    with (source_root / "Cargo.toml").open("rb") as source:
        version = tomllib.load(source)["workspace"]["package"]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(f"Invalid workspace version: {version!r}")
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError(f"Binary is missing or not executable: {binary}")
    reported = subprocess.run(
        [str(binary), "--version"], check=True, capture_output=True,
        text=True, timeout=30,
    ).stdout.strip()
    if reported != f"gitcomet {version}":
        raise ValueError(
            f"Binary version mismatch: expected 'gitcomet {version}', got {reported!r}"
        )
    commit = subprocess.run(
        ["git", "-C", str(source_root), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError(f"Invalid source commit: {commit!r}")

    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"gitcomet-{version}-native-titlebar-linux-x86_64.tar.gz"
    with tempfile.TemporaryDirectory(prefix="native-titlebar-", dir=output_dir) as temp:
        staging = Path(temp) / "gitcomet-native-titlebar"
        (staging / "bin").mkdir(parents=True)
        shutil.copy2(binary, staging / "bin/gitcomet")
        (staging / "bin/gitcomet").chmod(0o755)
        for name in ("install.py", "README.txt"):
            shutil.copy2(source_root / "scripts/native-titlebar" / name, staging / name)
        (staging / "install.py").chmod(0o755)
        source_archive = f"source/gitcomet-{version}-source.tar.gz"
        (staging / "source").mkdir()
        subprocess.run(
            ["git", "-C", str(source_root), "archive", "--format=tar.gz",
             f"--prefix=gitcomet-{version}/", f"--output={staging / source_archive}", commit],
            check=True,
        )
        (staging / "build-info.json").write_text(
            json.dumps({"schema": 1, "version": version, "commit": commit,
                        "target": TARGET, "source_archive": source_archive}, indent=2) + "\n",
            encoding="utf-8",
        )

        licenses = staging / "licenses"
        licenses.mkdir()
        for name in ("LICENSE-AGPL-3.0", "NOTICE"):
            shutil.copy2(source_root / name, licenses / name)
        assets = source_root / "crates/gitcomet-ui-gpui/assets"
        shutil.copy2(assets / "open_source_licenses.tsv", licenses)
        font_licenses = sorted((assets / "fonts").glob("*/LICENSE*"))
        if not font_licenses:
            raise ValueError("No bundled font licenses found")
        for source in font_licenses:
            destination = licenses / "fonts" / source.relative_to(assets / "fonts")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        for source in sorted((source_root / "vendor").glob("*/LICENSE*")):
            destination = licenses / "vendor" / source.relative_to(source_root / "vendor")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

        temporary_archive = Path(temp) / archive.name
        with tarfile.open(temporary_archive, "w:gz") as tar:
            tar.add(staging, arcname=staging.name)
        temporary_archive.replace(archive)
    with archive.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    archive.with_name(archive.name + ".sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="utf-8",
    )
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--binary", type=Path,
        default=SOURCE_ROOT / "target" / TARGET / "release/gitcomet",
    )
    parser.add_argument("--output-dir", type=Path, default=SOURCE_ROOT / "dist")
    args = parser.parse_args()
    try:
        print(package(SOURCE_ROOT, args.binary, args.output_dir))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Packaging failed: {error}\n")


if __name__ == "__main__":
    main()
