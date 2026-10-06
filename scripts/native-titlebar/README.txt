GitComet with optional native Linux titlebar

Source: https://github.com/xvAcid/GitComet
Update branch: https://github.com/xvAcid/GitComet/tree/native-titlebar
The exact source commit and application version are in build-info.json.
The complete matching source snapshot is in source/gitcomet-<version>-source.tar.gz.
This is a personal fork build. It is not an upstream release package.

Fedora / GNOME, x86_64:
  sudo dnf install libxkbcommon-x11
  python3 install.py

This installer updates existing user launchers for a GitComet Linux x86_64
AppImage and keeps launcher backups. The original AppImage is retained unless
its removal is requested with --remove-appimage. It recognizes
gitcomet.desktop and appimagekit_*-GitComet.desktop in your applications folder.
The original AppImage must not be newer than this bundle. RPM and other launcher
formats are not supported by this installer.

Close existing GitComet windows before installing or upgrading. Start GitComet
from its application-menu entry after installation. The installer creates a
wrapper that enables GITCOMET_NATIVE_TITLEBAR=1 and selects X11/XWayland for this
application. An X11/XWayland display must be available. The system draws the
window buttons; the application keeps its menu and repository tabs.

Check installed state:
  python3 install.py --status

Restore the previous launchers:
  python3 install.py --restore

Choose or move the program directory:
  python3 install.py --install-dir "$HOME/Application/GitComet"

Launch directly from that directory:
  "$HOME/Application/GitComet/run"

A newer standalone installer can use this extracted bundle with --bundle PATH.
The chosen program directory is remembered for later upgrades. Installation
state and original launcher backups stay in the user data directory.

The installer works in your user account and does not need sudo. To upgrade
this fork, extract a newer archive and run its install.py. To remove a specific
original AppImage after successful installation, pass --remove-appimage with
its absolute path. Restore requires that AppImage to be present again.

More details and the update workflow: docs/native-titlebar.md in the source repo.
Licenses and notices, including the embedded fonts, are in licenses/.
