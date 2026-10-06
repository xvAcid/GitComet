"""Exercise archive contents and version rejection without building the GUI."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    "native_titlebar_package", Path(__file__).with_name("package.py")
)
PACKAGING = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PACKAGING)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "source"
        self.root.mkdir()
        self.output = Path(self.temp.name) / "dist"
        files = {
            "Cargo.toml": '[workspace.package]\nversion = "0.2.6"\n',
            "scripts/native-titlebar/install.py": "# installer fixture\n",
            "scripts/native-titlebar/README.txt": "Read me\n",
            "LICENSE-AGPL-3.0": "Application license\n",
            "NOTICE": "Copyright notice\n",
            "crates/gitcomet-ui-gpui/assets/open_source_licenses.tsv": "licenses\n",
            "crates/gitcomet-ui-gpui/assets/fonts/example/LICENSE-OFL.txt": "font license\n",
            "vendor/example/LICENSE": "vendor license\n",
        }
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.git("init", "--quiet")
        self.git("add", ".")
        self.commit_source()
        self.commit = self.git("rev-parse", "HEAD").strip()
        self.binary = Path(self.temp.name) / "gitcomet"
        self.write_binary("0.2.6")

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.root), *args], check=True,
            capture_output=True, text=True,
        ).stdout

    def write_binary(self, version):
        self.binary.write_text(
            f"#!/bin/sh\nprintf '%s\\n' 'gitcomet {version}'\n", encoding="utf-8",
        )
        self.binary.chmod(0o755)

    def commit_source(self):
        self.git("-c", "user.name=Packaging Test", "-c",
                 "user.email=packaging@example.invalid", "commit", "--quiet", "-m", "Fixture")

    def test_archive_has_installer_metadata_binary_and_licenses(self):
        archive = PACKAGING.package(self.root, self.binary, self.output)
        self.assertEqual(archive.name, "gitcomet-0.2.6-native-titlebar-linux-x86_64.tar.gz")
        prefix = "gitcomet-native-titlebar/"
        with tarfile.open(archive, "r:gz") as tar:
            expected = {
                "bin/gitcomet", "install.py", "build-info.json", "README.txt",
                "licenses/LICENSE-AGPL-3.0", "licenses/NOTICE",
                "licenses/open_source_licenses.tsv",
                "licenses/fonts/example/LICENSE-OFL.txt",
                "licenses/vendor/example/LICENSE",
                "source/gitcomet-0.2.6-source.tar.gz",
            }
            self.assertEqual(
                {entry.name.removeprefix(prefix) for entry in tar if entry.isfile()},
                expected,
            )
            for name in ("bin/gitcomet", "install.py"):
                self.assertEqual(tar.getmember(prefix + name).mode & 0o777, 0o755)
            metadata = json.load(tar.extractfile(prefix + "build-info.json"))
            self.assertEqual(metadata, {
                "schema": 1, "version": "0.2.6", "commit": self.commit,
                "target": "x86_64-unknown-linux-gnu",
                "source_archive": "source/gitcomet-0.2.6-source.tar.gz",
            })
            self.assertEqual(tar.extractfile(prefix + "bin/gitcomet").read(), self.binary.read_bytes())
            snapshot = tar.extractfile(prefix + metadata["source_archive"]).read()
            with tarfile.open(fileobj=io.BytesIO(snapshot), mode="r:gz") as source:
                self.assertEqual(source.pax_headers["comment"], metadata["commit"])
                self.assertFalse(any(".git" in Path(name).parts for name in source.getnames()))
                self.assertEqual(
                    source.extractfile("gitcomet-0.2.6/Cargo.toml").read(),
                    (self.root / "Cargo.toml").read_bytes(),
                )
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.assertEqual(
            archive.with_name(archive.name + ".sha256").read_text(),
            f"{digest}  {archive.name}\n",
        )

    def test_wrong_binary_version_is_rejected_before_output(self):
        self.write_binary("0.2.5")
        with self.assertRaisesRegex(ValueError, "Binary version mismatch"):
            PACKAGING.package(self.root, self.binary, self.output)
        self.assertFalse(self.output.exists())

    def test_missing_installer_does_not_create_an_archive(self):
        (self.root / "scripts/native-titlebar/install.py").unlink()
        self.git("add", "-u")
        self.commit_source()
        with self.assertRaises(FileNotFoundError):
            PACKAGING.package(self.root, self.binary, self.output)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_binary_failure_is_rejected(self):
        self.binary.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
        with self.assertRaises(subprocess.CalledProcessError):
            PACKAGING.package(self.root, self.binary, self.output)
        self.assertFalse(self.output.exists())

    def test_uncommitted_tracked_source_is_rejected(self):
        (self.root / "NOTICE").write_text("Changed source\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Commit tracked source changes"):
            PACKAGING.package(self.root, self.binary, self.output)
        self.assertFalse(self.output.exists())

    def test_staged_source_is_rejected(self):
        (self.root / "NOTICE").write_text("Staged source\n", encoding="utf-8")
        self.git("add", "NOTICE")
        with self.assertRaisesRegex(ValueError, "Commit tracked source changes"):
            PACKAGING.package(self.root, self.binary, self.output)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
