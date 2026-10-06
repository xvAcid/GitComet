# Native Linux titlebar

This fork adds an opt-in native titlebar to GitComet. On Linux,
`GITCOMET_NATIVE_TITLEBAR=1` requests server decorations for the main window and
settings window. Custom caption buttons and the custom rounded frame disappear
only when the backend reports server decorations. The app menu and repository
tabs stay in place. With the variable unset, GitComet retains its usual behavior.

GNOME's native Wayland path may fall back to client decorations. The bundled
installer creates a launcher wrapper that enables the flag and selects
X11/XWayland for GitComet by unsetting `WAYLAND_DISPLAY` and `WAYLAND_SOCKET`.
It does not change the desktop session's environment. XWayland must be available.

## Build with GitHub Actions

The **Native titlebar Linux** workflow runs only when manually dispatched. It
builds the selected branch's exact commit, with Rust 1.98.1 and `Cargo.lock`, then
uploads an installable Linux x86_64 archive. The workflow token has read-only
repository permissions; the workflow does not create releases or push commits.

GitHub requires the workflow file to exist on the repository's default branch
before its **Run workflow** button is available. After publishing the
`native-titlebar` branch, open the fork's **Settings → Default branch**, select
`native-titlebar`, and confirm the change. Merely pushing the branch does not
change this setting. If GitHub asks you to enable Actions when you first open the
fork's Actions page, enable them for the fork.

1. Open the fork's **Actions → Native titlebar Linux → Run workflow**.
2. Select `native-titlebar` (or another branch containing the native titlebar
   changes), then start the workflow.
3. Download the artifact from the successful run. GitHub wraps the `.tar.gz` and
   checksum file in an artifact ZIP; extract that ZIP first.
4. Extract `gitcomet-<version>-native-titlebar-linux-x86_64.tar.gz`.

`build-info.json` records the application version, exact source commit, and target.
Packaging refuses a binary whose `--version` differs from `Cargo.toml`. GitHub
artifacts expire after 30 days, so keep a local copy of builds you use.
The archive includes the complete matching source snapshot under
`source/gitcomet-<version>-source.tar.gz`, without Git's internal repository data.
The online update branch is
<https://github.com/xvAcid/GitComet/tree/native-titlebar>.

## Install, upgrade, or restore on Fedora

The installer supports an existing GitComet Linux x86_64 AppImage installation
with a user launcher named `gitcomet.desktop` or
`appimagekit_*-GitComet.desktop`. The original AppImage must be no newer than the
bundle being installed. RPM and other launcher formats are not supported by this
installer. The original AppImage and GitComet's settings are preserved.

Install the X11 keyboard runtime library if needed:

```sh
sudo dnf install libxkbcommon-x11
```

Close running GitComet windows. From the extracted `gitcomet-native-titlebar`
directory, install the fork's binary and launcher for your user account:

```sh
python3 install.py
python3 install.py --status
```

Launch GitComet through the application menu so that its native-titlebar wrapper
runs. To
upgrade, extract a new archive and run the new `install.py`; the installer keeps
the original launcher backups. To restore those launchers:

```sh
python3 install.py --restore
```

For a temporary launch directly from a source build, use:

```sh
env -u WAYLAND_DISPLAY -u WAYLAND_SOCKET GITCOMET_NATIVE_TITLEBAR=1 \
  target/x86_64-unknown-linux-gnu/release/gitcomet
```

## Follow stable upstream releases

Keep the native changes on a dedicated branch and update from stable release
tags. The current base is upstream `v0.2.6`, commit
`d1689a51f86ed2f337f276656b4fa91742396d42`. The native titlebar patch transfers
unchanged from `v0.2.5` to `v0.2.6`.

From a clean local checkout of your fork, add the upstream remote once:

```sh
git remote add upstream https://github.com/Auto-Explore/GitComet.git
```

For a later release, replace `vX.Y.Z` below with its actual stable tag. Merge into
a temporary update branch, review any conflicts, and run the manual build before
merging the update into `native-titlebar`:

```sh
git fetch upstream --tags
git switch native-titlebar
git switch -c update-native-vX.Y.Z
git merge --no-ff vX.Y.Z
git diff --check
git push -u origin update-native-vX.Y.Z
```

Dispatch **Native titlebar Linux** for `update-native-vX.Y.Z`. If the workflow
passes, install its artifact and check both main and settings windows: system
buttons, dragging, resizing, maximizing, closing, and the preserved app menu and
repository tabs. Also check that launching without the flag retains the normal
appearance. Merge the reviewed update branch into `native-titlebar` afterward.

Review upstream changes to window construction, custom titlebar rendering,
decoration fallback, and GPUI's pinned revision. If the Rust version changes,
update the workflow's toolchain and cache key as well. Keep `Cargo.lock` from the
upstream release unless an intentional dependency change is required. A clean
merge confirms textual compatibility; it does not replace building and checking
the new version.

## Local build and packaging

Use the same native build dependencies listed in the workflow and Rust 1.98.1:
commit your tracked source changes before packaging. The packager rejects a
modified tracked tree so that the embedded source archive and commit agree.
Untracked build output is ignored. Build the binary from this same revision.

```sh
CARGO_BUILD_JOBS=2 CARGO_PROFILE_RELEASE_LTO=false \
  CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16 \
  cargo +1.98.1 build --locked --release --target x86_64-unknown-linux-gnu \
  -p gitcomet --bin gitcomet --no-default-features --features ui-gpui,gix
python3 -m unittest discover -s scripts/native-titlebar -p 'test_*.py'
python3 scripts/native-titlebar/package.py
```

The output is `dist/gitcomet-<version>-native-titlebar-linux-x86_64.tar.gz` plus
its SHA-256 checksum. Packaging uses Python 3.11 or newer and includes the
installer, provenance metadata, matching source archive, source notices, and
bundled font licenses.
