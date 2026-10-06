import contextlib
import ctypes
import ctypes.util
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent
INSTALLER = ROOT / 'install.py'
spec = importlib.util.spec_from_file_location('native_installer', INSTALLER)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class GError(ctypes.Structure):
    _fields_ = [('domain', ctypes.c_uint), ('code', ctypes.c_int), ('message', ctypes.c_char_p)]


def launch_with_gio(desktop):
    gio = ctypes.CDLL(ctypes.util.find_library('gio-2.0'))
    gio.g_desktop_app_info_new_from_filename.argtypes = [ctypes.c_char_p]
    gio.g_desktop_app_info_new_from_filename.restype = ctypes.c_void_p
    gio.g_app_info_launch.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.POINTER(ctypes.POINTER(GError))]
    gio.g_app_info_launch.restype = ctypes.c_int
    gio.g_object_unref.argtypes = [ctypes.c_void_p]
    app = gio.g_desktop_app_info_new_from_filename(os.fsencode(desktop))
    if not app:
        raise AssertionError('GLib rejected desktop file: ' + desktop.read_text())
    error = ctypes.POINTER(GError)()
    try:
        if not gio.g_app_info_launch(app, None, None, ctypes.byref(error)):
            raise AssertionError(error.contents.message.decode() if error else 'GLib launch failed')
    finally:
        gio.g_object_unref(app)


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gitcomet-installer-test-')
        self.root = Path(self.temp.name)
        self.bundle = self.root / 'Bundle With Spaces'
        self.bundle.mkdir()
        self.env = mock.patch.dict(os.environ, {
            'XDG_DATA_HOME': str(self.root / 'data with spaces'),
            'DISPLAY': ':fake-test-display',
            'WAYLAND_DISPLAY': 'fake-wayland',
            'WAYLAND_SOCKET': '321',
            'NATIVE_INSTALLER_TEST_OUTPUT': str(self.root / 'launched.json'),
        })
        self.env.start()
        self.bundle_patch = mock.patch.object(mod, 'BUNDLE', self.bundle)
        self.bundle_patch.start()
        self.log = io.StringIO()
        self.stdout = contextlib.redirect_stdout(self.log)
        self.stdout.__enter__()
        self.refresh = mock.patch.object(mod, 'refresh_desktop')
        self.refresh.start()
        self.make_binary()
        self.make_metadata()

    def tearDown(self):
        self.refresh.stop()
        self.stdout.__exit__(None, None, None)
        self.bundle_patch.stop()
        self.env.stop()
        self.temp.cleanup()

    def make_metadata(self, version='0.2.6'):
        (self.bundle / 'build-info.json').write_text(json.dumps({
            'schema': 1, 'version': version, 'commit': 'a' * 40,
            'target': 'x86_64-unknown-linux-gnu'}))

    def make_binary(self, version='0.2.6'):
        binary = self.bundle / 'bin/gitcomet'
        binary.parent.mkdir(exist_ok=True)
        binary.write_text('#!' + sys.executable + '\n'
            'import json, os, sys\n'
            'if sys.argv[1:] == ["--version"]:\n'
            '    print(' + repr('gitcomet ' + version) + ')\n'
            '    raise SystemExit(0)\n'
            'with open(os.environ["NATIVE_INSTALLER_TEST_OUTPUT"], "w") as stream:\n'
            '    json.dump({"argv": sys.argv[1:], "native": os.getenv("GITCOMET_NATIVE_TITLEBAR"), '
            '"wayland_display": os.getenv("WAYLAND_DISPLAY"), "wayland_socket": os.getenv("WAYLAND_SOCKET"), '
            '"display": os.getenv("DISPLAY")}, stream)\n')
        binary.chmod(0o755)

    def make_launcher(self, name='gitcomet.desktop', version='0.2.5', suffix=' %U'):
        _, appdir = mod.paths()
        appdir.mkdir(parents=True, exist_ok=True)
        image = self.root / 'Application Folder' / ('gitcomet-v' + version + '-linux-x86_64_deadbeef.AppImage')
        image.parent.mkdir(exist_ok=True)
        image.write_bytes(b'Original AppImage is not executed or changed.\n')
        image.chmod(0o755)
        desktop = appdir / name
        original = ('# retained comment\n[Desktop Entry]\nType=Application\nName=GitComet\n'
                    'Exec="' + str(image) + '"' + suffix + '\n'
                    'TryExec=' + str(image) + '\nDBusActivatable=true\nIcon=gitcomet\n'
                    '[Desktop Action Test]\nName=Other section\nExec=/bin/true\n').encode()
        desktop.write_bytes(original)
        desktop.chmod(0o640)
        return desktop, original, image

    def legacy_install(self):
        """Exact schema and wrapper emitted by the earlier 0.2.5 installer."""
        entries = [self.make_launcher(suffix=' --existing "two words" %U'),
                   self.make_launcher('appimagekit_a123-GitComet.desktop')]
        base, _ = mod.paths()
        (base / 'bin').mkdir(parents=True)
        (base / 'backups').mkdir()
        ready = (self.bundle / 'bin/gitcomet').read_bytes()
        self.make_binary(version='0.2.5')
        installed = base / 'bin/gitcomet'
        installed.write_bytes((self.bundle / 'bin/gitcomet').read_bytes())
        installed.chmod(0o755)
        (self.bundle / 'bin/gitcomet').write_bytes(ready)
        import shlex
        wrapper = ('#!/bin/sh\n'
                   'if [ -z "${DISPLAY:-}" ]; then\n'
                   "  echo 'GitComet: XWayland DISPLAY is unavailable.' >&2\n"
                   '  exit 1\nfi\n'
                   'unset WAYLAND_DISPLAY WAYLAND_SOCKET\n'
                   'export GITCOMET_NATIVE_TITLEBAR=1\n'
                   'exec ' + shlex.quote(str(installed)) + ' "$@"\n').encode()
        (base / 'run').write_bytes(wrapper)
        (base / 'run').chmod(0o755)
        records = []
        for path, original, _ in entries:
            modified = mod.patched_desktop(original, base / 'run', '0.2.5')
            backup = base / 'backups' / (mod.digest(str(path).encode())[:16] + '.desktop')
            backup.write_bytes(original)
            backup.chmod(0o600)
            records.append({'path': str(path), 'backup': str(backup), 'mode': 0o640,
                            'original_sha256': mod.digest(original),
                            'installed_sha256': mod.digest(modified)})
            path.write_bytes(modified)
        state = {'schema': 1, 'version': '0.2.5', 'active': True,
                 'binary_sha256': mod.file_digest(installed), 'files': records}
        (base / 'state.json').write_text(json.dumps(state))
        (base / 'state.json').chmod(0o600)
        return entries, state

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
                for p in self.root.rglob('*') if p.is_file()}

    def test_apply_and_restore_multiple_launchers(self):
        entries = [self.make_launcher(), self.make_launcher('appimagekit_a123-GitComet.desktop')]
        originals = {image: image.read_bytes() for _, _, image in entries}
        mod.apply()
        base, _ = mod.paths()
        state = mod.load_state(base)
        self.assertTrue(state['active'])
        self.assertEqual(len(state['files']), 2)
        for path, original, _ in entries:
            self.assertNotEqual(path.read_bytes(), original)
            self.assertIn(b'DBusActivatable=false\n', path.read_bytes())
            self.assertIn(b'[Desktop Action Test]\nName=Other section\nExec=/bin/true\n', path.read_bytes())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        subprocess.run([str(base / 'run'), 'repo with spaces', '$literal', '%literal'], check=True)
        recorded = json.loads((self.root / 'launched.json').read_text())
        self.assertEqual(recorded['argv'], ['repo with spaces', '$literal', '%literal'])
        self.assertEqual(recorded['native'], '1')
        self.assertIsNone(recorded['wayland_display'])
        self.assertIsNone(recorded['wayland_socket'])
        self.assertEqual(recorded['display'], ':fake-test-display')
        installed_bytes = {p: p.read_bytes() for p, _, _ in entries}
        mod.apply()
        self.assertEqual(installed_bytes, {p: p.read_bytes() for p, _, _ in entries})
        mod.restore()
        self.assertFalse(mod.load_state(base)['active'])
        for path, original, _ in entries:
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        self.assertEqual(originals, {p: p.read_bytes() for p in originals})

    def test_rejects_wrong_appimage_version(self):
        path, original, _ = self.make_launcher(version='0.2.7')
        with self.assertRaises(mod.Error):
            mod.apply()
        self.assertEqual(path.read_bytes(), original)
        self.assertIsNone(mod.load_state(mod.paths()[0]))

    def test_rejects_wrong_binary_version_before_desktop_changes(self):
        path, original, _ = self.make_launcher()
        self.make_binary(version='0.2.7')
        with self.assertRaises(mod.Error):
            mod.apply()
        self.assertEqual(path.read_bytes(), original)
        self.assertIsNone(mod.load_state(mod.paths()[0]))

    def test_changed_launcher_blocks_restore_before_any_write(self):
        first, _, _ = self.make_launcher()
        second, _, _ = self.make_launcher('appimagekit_x-GitComet.desktop')
        mod.apply()
        second.write_bytes(second.read_bytes() + b'# a user edit\n')
        before = {p: p.read_bytes() for p in (first, second)}
        with self.assertRaises(mod.Error):
            mod.restore()
        self.assertEqual(before, {p: p.read_bytes() for p in (first, second)})
        with self.assertRaises(mod.Error):
            mod.apply()

    def test_partial_desktop_update_is_recoverable(self):
        first, first_original, _ = self.make_launcher()
        second, second_original, _ = self.make_launcher('appimagekit_x-GitComet.desktop')
        atomic = mod.write_atomic
        def fail_second(path, data, mode=0o644):
            if path == second:
                raise OSError('simulated write failure')
            return atomic(path, data, mode)
        with mock.patch.object(mod, 'write_atomic', side_effect=fail_second):
            with self.assertRaises(OSError):
                mod.apply()
        self.assertNotEqual(first.read_bytes(), first_original)
        self.assertEqual(second.read_bytes(), second_original)
        mod.restore()
        self.assertEqual(first.read_bytes(), first_original)
        self.assertEqual(second.read_bytes(), second_original)

    @unittest.skipUnless(ctypes.util.find_library("gio-2.0"), "libgio is unavailable")
    def test_real_glib_exec_parser_spaces(self):
        desktop, _, _ = self.make_launcher(suffix=' --test "literal with spaces" %U')
        mod.apply()
        launch_with_gio(desktop)
        output = self.root / 'launched.json'
        deadline = time.monotonic() + 3
        while not output.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(output.exists())
        self.assertEqual(json.loads(output.read_text())['argv'], ['--test', 'literal with spaces'])

    @unittest.skipUnless(ctypes.util.find_library("gio-2.0"), "libgio is unavailable")
    def test_real_glib_exec_parser_special_characters_in_output_path(self):
        os.environ['XDG_DATA_HOME'] = str(self.root / 'data $ ` " \\ special')
        desktop, _, _ = self.make_launcher(suffix=' %U')
        mod.apply()
        launch_with_gio(desktop)
        output = self.root / 'launched.json'
        deadline = time.monotonic() + 3
        while not output.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(output.exists())
        self.assertEqual(json.loads(output.read_text())['native'], '1')

    def test_unsupported_data_path_aborts_before_writes(self):
        desktop, original, image = self.make_launcher()
        image_original = image.read_bytes()
        for char in ('%', '\n', '\r'):
            with self.subTest(char=repr(char)):
                unsupported = self.root / ('data ' + char + ' special')
                os.environ['XDG_DATA_HOME'] = str(unsupported)
                with self.assertRaises(mod.Error):
                    mod.apply()
                self.assertFalse(unsupported.exists())
                self.assertEqual(desktop.read_bytes(), original)
                self.assertEqual(image.read_bytes(), image_original)

    def test_fresh_install_accepts_same_version_appimage(self):
        desktop, original, _ = self.make_launcher(version='0.2.6')
        mod.apply()
        self.assertNotEqual(desktop.read_bytes(), original)
        mod.restore()
        self.assertEqual(desktop.read_bytes(), original)

    def test_legacy_upgrade_preserves_original_backups_and_restores_exactly(self):
        entries, old_state = self.legacy_install()
        base, _ = mod.paths()
        old_backups = {Path(r['backup']): Path(r['backup']).read_bytes() for r in old_state['files']}
        old_images = {image: image.read_bytes() for _, _, image in entries}
        old_launchers = {path: path.read_bytes() for path, _, _ in entries}
        mod.apply()
        state = mod.load_state(base)
        self.assertEqual(state['version'], '0.2.6')
        self.assertEqual(state['files'], old_state['files'])
        self.assertEqual(state['binary_sha256'], mod.file_digest(self.bundle / 'bin/gitcomet'))
        self.assertEqual(state['wrapper_sha256'], mod.file_digest(base / 'run'))
        self.assertNotIn('installing', state)
        self.assertEqual(old_backups, {p: p.read_bytes() for p in old_backups})
        self.assertEqual(old_launchers, {p: p.read_bytes() for p in old_launchers})
        self.assertEqual(old_images, {p: p.read_bytes() for p in old_images})
        subprocess.run([str(base / 'run'), 'repository with spaces'], check=True)
        self.assertEqual(json.loads((self.root / 'launched.json').read_text())['native'], '1')
        # Restore remains available even if the downloaded bundle's payload is gone.
        (self.bundle / 'build-info.json').unlink()
        (self.bundle / 'bin/gitcomet').unlink()
        mod.restore()
        for path, original, _ in entries:
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        self.assertEqual(old_images, {p: p.read_bytes() for p in old_images})

    def test_restore_understands_legacy_state_without_upgrade(self):
        entries, _ = self.legacy_install()
        (self.bundle / 'build-info.json').unlink()
        mod.restore()
        for path, original, _ in entries:
            self.assertEqual(path.read_bytes(), original)

    def test_upgrade_refuses_changed_owned_files_before_any_write(self):
        self.legacy_install()
        base, _ = mod.paths()
        for relative in ('bin/gitcomet', 'run'):
            with self.subTest(relative=relative):
                path = base / relative
                original = path.read_bytes()
                path.write_bytes(original + b'\n# external change\n')
                before = self.snapshot()
                with self.assertRaisesRegex(mod.Error, 'installed file has changed'):
                    mod.apply()
                self.assertEqual(before, self.snapshot())
                path.write_bytes(original)

    def test_upgrade_rejects_wrong_binary_without_changing_legacy_install(self):
        self.legacy_install()
        self.make_binary(version='0.2.7')
        before = self.snapshot()
        with self.assertRaisesRegex(mod.Error, 'version check'):
            mod.apply()
        self.assertEqual(before, self.snapshot())

    def test_upgrade_includes_a_new_appimagelauncher_entry(self):
        entries, old_state = self.legacy_install()
        added = self.make_launcher('appimagekit_new-GitComet.desktop')
        mod.apply()
        state = mod.load_state(mod.paths()[0])
        self.assertEqual(state['files'][:2], old_state['files'])
        self.assertEqual(len(state['files']), 3)
        mod.restore()
        for path, original, _ in entries + [added]:
            self.assertEqual(path.read_bytes(), original)

    def test_interrupted_binary_upgrade_can_be_retried(self):
        self.legacy_install()
        base, _ = mod.paths()
        original_copy = mod.copy_binary
        for after_copy in (False, True):
            with self.subTest(after_copy=after_copy):
                # Both failure points leave the journal containing only known hashes.
                def failing_copy(*args):
                    if after_copy:
                        original_copy(*args)
                    raise OSError('simulated interrupted binary replacement')
                with mock.patch.object(mod, 'copy_binary', side_effect=failing_copy):
                    with self.assertRaises(OSError):
                        mod.apply()
                self.assertTrue(mod.load_state(base)['installing'])
                mod.status()
                mod.apply()
                self.assertNotIn('installing', mod.load_state(base))
                self.assertEqual(mod.file_digest(base / 'bin/gitcomet'), mod.file_digest(self.bundle / 'bin/gitcomet'))
                # Force a different, still correctly versioned build for the next case.
                with (self.bundle / 'bin/gitcomet').open('a') as stream:
                    stream.write('\n# next reproducible build\n')

    def test_interrupted_upgrade_can_restore_the_original_appimage(self):
        entries, _ = self.legacy_install()
        original_copy = mod.copy_binary
        def fail_after_copy(*args):
            original_copy(*args)
            raise OSError('simulated interruption')
        with mock.patch.object(mod, 'copy_binary', side_effect=fail_after_copy):
            with self.assertRaises(OSError):
                mod.apply()
        mod.restore()
        for path, original, _ in entries:
            self.assertEqual(path.read_bytes(), original)
        # A later install also recovers cleanly after that restore.
        mod.apply()
        self.assertFalse(mod.load_state(mod.paths()[0]).get('installing', False))

    def test_partial_fresh_install_can_be_retried(self):
        self.make_launcher()
        second, _, _ = self.make_launcher('appimagekit_x-GitComet.desktop')
        atomic = mod.write_atomic
        def fail_second(path, data, mode=0o644):
            if path == second:
                raise OSError('simulated launcher write failure')
            return atomic(path, data, mode)
        with mock.patch.object(mod, 'write_atomic', side_effect=fail_second):
            with self.assertRaises(OSError):
                mod.apply()
        mod.apply()
        self.assertFalse(mod.load_state(mod.paths()[0]).get('installing', False))

    def test_modified_backup_blocks_upgrade_and_restore(self):
        _, state = self.legacy_install()
        backup = Path(state['files'][0]['backup'])
        backup.write_bytes(backup.read_bytes() + b'# external change\n')
        before = self.snapshot()
        for action in (mod.apply, mod.restore):
            with self.assertRaisesRegex(mod.Error, 'backup has changed'):
                action()
            self.assertEqual(before, self.snapshot())

    def test_status_is_read_only_and_does_not_require_payload_or_display(self):
        self.legacy_install()
        (self.bundle / 'bin/gitcomet').unlink()
        (self.bundle / 'build-info.json').unlink()
        os.environ.pop('DISPLAY')
        before = self.snapshot()
        with mock.patch.object(mod.subprocess, 'run', side_effect=AssertionError('status ran a subprocess')):
            mod.status()
        self.assertEqual(before, self.snapshot())
        self.assertIn('0.2.5', self.log.getvalue())

    def test_active_installation_cannot_be_downgraded(self):
        self.make_launcher()
        mod.apply()
        self.make_metadata(version='0.2.5')
        self.make_binary(version='0.2.5')
        before = self.snapshot()
        with self.assertRaisesRegex(mod.Error, 'downgrade'):
            mod.apply()
        self.assertEqual(before, self.snapshot())

    def test_bad_metadata_is_rejected_before_any_write(self):
        self.make_launcher()
        for info in ({'schema': 2}, {'schema': 1, 'version': '0.2.6', 'commit': 'not-a-commit'},
                     {'schema': 1, 'version': '0.2.6', 'commit': 'a' * 41},
                     {'schema': 1, 'version': '0.2.6', 'commit': 'a' * 40, 'target': 'aarch64-unknown-linux-gnu'}):
            with self.subTest(info=info):
                (self.bundle / 'build-info.json').write_text(json.dumps(info))
                before = self.snapshot()
                with self.assertRaises(mod.Error):
                    mod.apply()
                self.assertEqual(before, self.snapshot())

    def test_default_cli_installs_and_root_is_rejected(self):
        self.make_launcher()
        with mock.patch.object(sys, 'argv', ['install.py']), mock.patch.object(mod.os, 'geteuid', return_value=0):
            with self.assertRaisesRegex(mod.Error, 'without sudo'):
                mod.main()
        self.assertIsNone(mod.load_state(mod.paths()[0]))
        with mock.patch.object(sys, 'argv', ['install.py']), mock.patch.object(mod.os, 'geteuid', return_value=1000):
            self.assertEqual(mod.main(), 0)
        self.assertTrue(mod.load_state(mod.paths()[0])['active'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
