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
            '    json.dump({"executable": sys.argv[0], "argv": sys.argv[1:], "native": os.getenv("GITCOMET_NATIVE_TITLEBAR"), '
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

    def test_separate_installer_bundle_and_fresh_custom_runtime(self):
        desktop, original, appimage = self.make_launcher(suffix=' --existing "two words" %U')
        destination = self.root / 'Application' / 'GitComet'
        installer_only = self.root / 'Downloaded installer'
        installer_only.mkdir()
        argv = ['install.py', '--bundle', str(self.bundle), '--install-dir', str(destination),
                '--remove-appimage', str(appimage)]
        with mock.patch.object(mod, 'BUNDLE', installer_only), mock.patch.object(sys, 'argv', argv), \
                mock.patch.object(mod.os, 'geteuid', return_value=1000):
            self.assertEqual(mod.main(), 0)
        base, _ = mod.paths()
        state = mod.load_state(base)
        self.assertEqual(state['install_dir'], str(destination))
        self.assertEqual(Path(state['files'][0]['backup']).read_bytes(), original)
        self.assertFalse(appimage.exists())
        self.assertFalse((base / 'bin/gitcomet').exists())
        self.assertFalse((base / 'run').exists())
        self.assertFalse((destination / 'run').is_symlink())
        self.assertFalse((destination / 'bin/gitcomet').is_symlink())
        self.assertEqual(mod.desktop_fields(desktop.read_bytes())['Icon'], 'gitcomet')
        self.assertIn(b' --existing "two words" %U\n', desktop.read_bytes())
        subprocess.run([str(destination / 'run'), 'repository with spaces'], check=True)
        result = json.loads((self.root / 'launched.json').read_text())
        self.assertEqual(result['executable'], str(destination / 'bin/gitcomet'))
        self.assertEqual(result['native'], '1')
        self.assertIsNone(result['wayland_display'])
        self.assertEqual(result['argv'], ['repository with spaces'])

    def test_relocates_legacy_runtime_and_preserves_original_backups(self):
        entries, legacy = self.legacy_install()
        base, _ = mod.paths()
        destination = self.root / 'Application' / 'GitComet'
        mod.apply(install_dir=destination)
        state = mod.load_state(base)
        self.assertEqual(state['version'], '0.2.6')
        self.assertEqual(state['install_dir'], str(destination))
        self.assertFalse((base / 'run').exists())
        self.assertFalse((base / 'bin/gitcomet').exists())
        self.assertTrue((base / 'state.json').is_file())
        for old, new in zip(legacy['files'], state['files']):
            self.assertNotEqual(old['installed_sha256'], new['installed_sha256'])
            self.assertEqual({k: v for k, v in old.items() if k != 'installed_sha256'},
                             {k: v for k, v in new.items() if k != 'installed_sha256'})
        for desktop, original, appimage in entries:
            self.assertTrue(appimage.is_file())  # Moving alone never authorizes removal.
            self.assertIn(str(destination / 'run'), desktop.read_text())
        mod.restore()
        for desktop, original, _ in entries:
            self.assertEqual(desktop.read_bytes(), original)

    def test_move_delete_then_future_upgrade_remembers_runtime_directory(self):
        desktop, original, appimage = self.make_launcher()
        other, _, other_appimage = self.make_launcher('appimagekit_other-GitComet.desktop', version='0.2.6')
        mod.apply()
        base, _ = mod.paths()
        # The previously shipped 0.2.6 installer did not record install_dir.
        previous = mod.load_state(base)
        previous.pop('install_dir')
        mod.save_state(base, previous)
        destination = self.root / 'Application' / 'GitComet'
        other_contents = other_appimage.read_bytes()
        mod.apply(install_dir=destination, remove_appimage=appimage)
        self.assertFalse(appimage.exists())
        self.assertEqual(other_appimage.read_bytes(), other_contents)
        self.assertFalse((base / 'run').exists())
        self.assertFalse((base / 'bin/gitcomet').exists())
        self.make_metadata(version='0.2.7')
        self.make_binary(version='0.2.7')
        mod.apply()  # No flags: retains the selected runtime, despite missing AppImage.
        state = mod.load_state(base)
        self.assertEqual(state['install_dir'], str(destination))
        self.assertEqual(state['version'], '0.2.7')
        self.assertEqual(mod.file_digest(destination / 'bin/gitcomet'), mod.file_digest(self.bundle / 'bin/gitcomet'))
        self.assertIn(str(destination / 'run'), desktop.read_text())
        self.assertIn(str(destination / 'run'), other.read_text())
        mod.status()
        before = self.snapshot()
        with self.assertRaisesRegex(mod.Error, 'Cannot restore.*AppImage is missing'):
            mod.restore()
        self.assertEqual(before, self.snapshot())
        self.assertIn('Icon=gitcomet\n', original.decode())

    def test_remove_appimage_rejects_unmanaged_path_and_symlink(self):
        _, _, appimage = self.make_launcher()
        other = self.root / 'unmanaged.AppImage'
        other.write_bytes(b'not managed')
        for request in (other, Path('relative.AppImage'), self.root / '*.AppImage'):
            with self.subTest(request=request):
                before = self.snapshot()
                with self.assertRaises(mod.Error):
                    mod.apply(remove_appimage=request)
                self.assertEqual(before, self.snapshot())
        appimage.unlink()
        appimage.symlink_to(other)
        with self.assertRaisesRegex(mod.Error, 'regular AppImage'):
            mod.apply(remove_appimage=appimage)
        self.assertTrue(appimage.is_symlink())
        self.assertEqual(other.read_bytes(), b'not managed')
        self.assertIsNone(mod.load_state(mod.paths()[0]))

    def test_remove_appimage_rejects_surviving_icon_or_action_references(self):
        desktop, original, appimage = self.make_launcher()
        variants = [
            original.replace(b'Icon=gitcomet', ('Icon=' + str(appimage)).encode()),
            original + ('[Desktop Action Another]\nName=Old binary\nExec="' + str(appimage) + '" --action\n').encode(),
            original + ('[Desktop Action Another]\nName=Old binary\nTryExec=' + str(appimage) + '\nExec=/bin/true\n').encode(),
        ]
        for data in variants:
            with self.subTest(data=data):
                desktop.write_bytes(data)
                before = self.snapshot()
                with self.assertRaisesRegex(mod.Error, 'still references'):
                    mod.apply(remove_appimage=appimage)
                self.assertEqual(before, self.snapshot())
        self.assertTrue(appimage.exists())

    def test_appimage_is_not_removed_when_installed_version_check_fails(self):
        desktop, original, appimage = self.make_launcher()
        destination = self.root / 'Application' / 'GitComet'
        verify = mod.verified_binary
        def fail_installed(info, directory=None):
            if directory == destination:
                raise mod.Error('simulated installed version check failure')
            return verify(info, directory)
        with mock.patch.object(mod, 'verified_binary', side_effect=fail_installed):
            with self.assertRaisesRegex(mod.Error, 'installed version check failure'):
                mod.apply(install_dir=destination, remove_appimage=appimage)
        self.assertTrue(appimage.exists())
        self.assertEqual(desktop.read_bytes(), original)
        mod.apply(remove_appimage=appimage)
        self.assertFalse(appimage.exists())

    def test_appimage_changed_during_install_is_not_removed(self):
        _, _, appimage = self.make_launcher()
        copy = mod.copy_binary
        changed = b'new external AppImage contents'
        def change_original(*args):
            copy(*args)
            appimage.write_bytes(changed)
        with mock.patch.object(mod, 'copy_binary', side_effect=change_original):
            with self.assertRaisesRegex(mod.Error, 'AppImage changed during installation'):
                mod.apply(install_dir=self.root / 'Application/GitComet', remove_appimage=appimage)
        self.assertEqual(appimage.read_bytes(), changed)

    def test_move_refuses_changed_source_and_occupied_destination(self):
        self.legacy_install()
        base, _ = mod.paths()
        destination = self.root / 'Application/GitComet'
        old_binary = base / 'bin/gitcomet'
        old_contents = old_binary.read_bytes()
        old_binary.write_bytes(old_contents + b'\n# external edit\n')
        before = self.snapshot()
        with self.assertRaisesRegex(mod.Error, 'installed file has changed'):
            mod.apply(install_dir=destination)
        self.assertEqual(before, self.snapshot())
        old_binary.write_bytes(old_contents)
        (destination / 'bin').mkdir(parents=True)
        (destination / 'bin/gitcomet').write_bytes(b'unowned file')
        before = self.snapshot()
        with self.assertRaisesRegex(mod.Error, 'untracked installation file'):
            mod.apply(install_dir=destination)
        self.assertEqual(before, self.snapshot())

    def test_move_partial_launcher_update_retries_then_cleans_old_runtime(self):
        entries, _ = self.legacy_install()
        base, _ = mod.paths()
        destination = self.root / 'Application/GitComet'
        second = entries[1][0]
        appimage = entries[0][2]
        atomic = mod.write_atomic
        def fail_second(path, data, mode=0o644):
            if path == second:
                raise OSError('simulated partial move')
            return atomic(path, data, mode)
        with mock.patch.object(mod, 'write_atomic', side_effect=fail_second):
            with self.assertRaises(OSError):
                mod.apply(install_dir=destination, remove_appimage=appimage)
        self.assertTrue((base / 'run').is_file())
        self.assertTrue((base / 'bin/gitcomet').is_file())
        self.assertTrue(appimage.is_file())
        self.assertIn(str(destination / 'run'), entries[0][0].read_text())
        self.assertIn(str(base / 'run'), second.read_text())
        mod.status()
        mod.apply(remove_appimage=appimage)
        self.assertFalse((base / 'run').exists())
        self.assertFalse((base / 'bin/gitcomet').exists())
        self.assertFalse(appimage.exists())
        state = mod.load_state(base)
        self.assertNotIn('moved_from', state)
        self.assertNotIn('installing', state)
        self.assertTrue(all('previous_installed_sha256' not in r for r in state['files']))
        # Repeating the explicit request is safe once the exact original is absent.
        mod.apply(remove_appimage=appimage)

    def test_move_interrupted_after_binary_copy_retries(self):
        self.legacy_install()
        base, _ = mod.paths()
        destination = self.root / 'Application/GitComet'
        copy = mod.copy_binary
        def fail_after_copy(*args):
            copy(*args)
            raise OSError('simulated copy interruption')
        with mock.patch.object(mod, 'copy_binary', side_effect=fail_after_copy):
            with self.assertRaises(OSError):
                mod.apply(install_dir=destination)
        self.assertFalse((destination / 'run').exists())
        self.assertTrue((base / 'run').exists())
        mod.status()
        mod.apply()
        self.assertTrue((destination / 'run').is_file())
        self.assertFalse((base / 'run').exists())

    def test_move_cleanup_refuses_source_changed_after_preflight(self):
        entries, _ = self.legacy_install()
        base, _ = mod.paths()
        destination = self.root / 'Application/GitComet'
        old_wrapper = (base / 'run').read_bytes()
        atomic = mod.write_atomic
        def change_old_wrapper(path, data, mode=0o644):
            result = atomic(path, data, mode)
            if path == entries[-1][0]:
                (base / 'run').write_bytes(old_wrapper + b'\n# external edit\n')
            return result
        with mock.patch.object(mod, 'write_atomic', side_effect=change_old_wrapper):
            with self.assertRaisesRegex(mod.Error, 'previous installation file has changed'):
                mod.apply(install_dir=destination)
        self.assertTrue((base / 'bin/gitcomet').exists())
        self.assertEqual((base / 'run').read_bytes(), old_wrapper + b'\n# external edit\n')
        (base / 'run').write_bytes(old_wrapper)
        mod.apply()
        self.assertFalse((base / 'run').exists())

    def test_move_keeps_old_runtime_if_new_wrapper_disappears_before_cleanup(self):
        entries, _ = self.legacy_install()
        base, _ = mod.paths()
        destination = self.root / 'Application/GitComet'
        atomic = mod.write_atomic
        def remove_new_wrapper(path, data, mode=0o644):
            result = atomic(path, data, mode)
            if path == entries[-1][0]:
                (destination / 'run').unlink()
            return result
        with mock.patch.object(mod, 'write_atomic', side_effect=remove_new_wrapper):
            with self.assertRaisesRegex(mod.Error, 'installed file has changed'):
                mod.apply(install_dir=destination, remove_appimage=entries[0][2])
        self.assertTrue((base / 'run').is_file())
        self.assertTrue((base / 'bin/gitcomet').is_file())
        self.assertTrue(entries[0][2].is_file())

    def test_partial_move_can_restore_original_launchers(self):
        entries, _ = self.legacy_install()
        destination = self.root / 'Application/GitComet'
        atomic = mod.write_atomic
        def fail_second(path, data, mode=0o644):
            if path == entries[1][0]:
                raise OSError('simulated partial move')
            return atomic(path, data, mode)
        with mock.patch.object(mod, 'write_atomic', side_effect=fail_second):
            with self.assertRaises(OSError):
                mod.apply(install_dir=destination)
        mod.restore()
        for desktop, original, _ in entries:
            self.assertEqual(desktop.read_bytes(), original)

    def test_install_directory_file_or_unsupported_path_is_rejected_before_writes(self):
        self.make_launcher()
        occupied = self.root / 'regular file'
        occupied.write_bytes(b'keep this file')
        for destination in (occupied, self.root / 'bad%path', Path('relative/path')):
            with self.subTest(destination=destination):
                before = self.snapshot()
                with self.assertRaises(mod.Error):
                    mod.apply(install_dir=destination)
                self.assertEqual(before, self.snapshot())

    def appimagelauncher_fixture(self, prefixed=True, lite=False):
        desktop, original, appimage = self.make_launcher('appimagekit_ail-GitComet.desktop')
        data = b''.join(line for line in original.splitlines(keepends=True) if not line.startswith(b'TryExec='))
        ids = ['AppImageLauncher-Remove-AppImage', 'AppImageLauncher-Update-AppImage'] if prefixed else ['Remove', 'Update']
        data = data.replace(b'Icon=gitcomet\n', ('Icon=gitcomet\nX-AppImage-Identifier=keep-this-id\nActions=Test;' + ';'.join(ids) + ';\n').encode())
        for action_id, verb in zip(ids, ['remove', 'update']):
            helper = ('/home/example/.local/lib/appimagelauncher-lite/appimagelauncher-lite.AppImage ' + verb
                      if lite else '/usr/lib64/appimagelauncher/' + verb)
            data += ('[Desktop Action ' + action_id + ']\nName=AppImage action\nName[ru]=Translated name\n'
                     'Icon=AppImageLauncher\nExec=' + helper + ' "' + str(appimage) + '"\n').encode()
        desktop.write_bytes(data)
        return desktop, data, appimage, ids

    def test_standard_appimagelauncher_actions_are_removed_and_stay_removed(self):
        for case, (prefixed, lite) in enumerate(((True, False), (False, False), (True, True), (False, True))):
            with self.subTest(prefixed=prefixed, lite=lite), \
                    mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(self.root / ('data-case-' + str(case)))}):
                desktop, original, appimage, ids = self.appimagelauncher_fixture(prefixed, lite)
                destination = self.root / 'Application' / ('GitComet-' + str(case))
                mod.apply(install_dir=destination, remove_appimage=appimage)
                data = desktop.read_bytes()
                fields = mod.desktop_fields(data)
                self.assertFalse(appimage.exists())
                self.assertEqual(fields['TryExec'], str(destination / 'run'))
                self.assertTrue(Path(fields['TryExec']).is_file())
                self.assertEqual(fields['Actions'], 'Test;')
                self.assertEqual(fields['Icon'], 'gitcomet')
                self.assertEqual(fields['X-AppImage-Identifier'], 'keep-this-id')
                self.assertIn(b'[Desktop Action Test]\nName=Other section\nExec=/bin/true\n', data)
                for action_id in ids:
                    self.assertNotIn(('[Desktop Action ' + action_id + ']').encode(), data)
                state = mod.load_state(mod.paths()[0])
                self.assertEqual(Path(state['files'][0]['backup']).read_bytes(), original)
                self.assertTrue(state['files'][0]['appimage_actions_removed'])
                with (self.bundle / 'bin/gitcomet').open('a') as stream:
                    stream.write('\n# updated build\n')
                mod.apply()  # Never recreate obsolete actions from the original backup.
                self.assertEqual(desktop.read_bytes(), data)

    def test_same_directory_same_binary_still_applies_action_cleanup(self):
        desktop, original, appimage, ids = self.appimagelauncher_fixture()
        mod.apply()
        self.assertTrue(appimage.exists())
        self.assertIn(('[Desktop Action ' + ids[0] + ']').encode(), desktop.read_bytes())
        mod.apply(remove_appimage=appimage)
        self.assertFalse(appimage.exists())
        self.assertEqual(mod.desktop_fields(desktop.read_bytes())['Actions'], 'Test;')
        self.assertEqual(Path(mod.load_state(mod.paths()[0])['files'][0]['backup']).read_bytes(), original)

    def test_same_directory_upgrade_adds_tryexec_missing_in_previous_installer(self):
        desktop, original, _ = self.make_launcher()
        desktop.write_bytes(b''.join(line for line in original.splitlines(keepends=True) if not line.startswith(b'TryExec=')))
        mod.apply()
        # Reproduce an earlier installed desktop lacking TryExec, with its recorded hash.
        old_installed = b''.join(line for line in desktop.read_bytes().splitlines(keepends=True) if not line.startswith(b'TryExec='))
        desktop.write_bytes(old_installed)
        base, _ = mod.paths()
        state = mod.load_state(base)
        state['files'][0]['installed_sha256'] = mod.digest(old_installed)
        mod.save_state(base, state)
        mod.apply()
        self.assertEqual(mod.desktop_fields(desktop.read_bytes())['TryExec'], str(base / 'run'))
        self.assertEqual(mod.load_state(base)['files'][0]['installed_sha256'], mod.file_digest(desktop))

    def test_custom_generic_remove_update_actions_are_preserved(self):
        desktop, _, appimage, _ = self.appimagelauncher_fixture()
        custom = b'[Desktop Action Remove]\nName=Custom remove\nExec=/bin/true\n[Desktop Action Update]\nName=Custom update\nExec=/bin/true\n'
        desktop.write_bytes(desktop.read_bytes().replace(b'Actions=Test;', b'Actions=Test;Remove;Update;') + custom)
        mod.apply(remove_appimage=appimage)
        self.assertIn(custom, desktop.read_bytes())
        self.assertEqual(mod.desktop_fields(desktop.read_bytes())['Actions'], 'Test;Remove;Update;')


if __name__ == '__main__':
    unittest.main(verbosity=2)
