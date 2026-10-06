#!/usr/bin/env python3
"""Install a bundled GitComet build with a reversible native-titlebar launcher.

Run as the desktop user, without sudo. Requires only the Python standard library.
GitComet's own settings are never modified. An original AppImage is removed only
when its exact path is explicitly supplied with --remove-appimage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile

BUNDLE = Path(__file__).resolve().parent
SHA256 = re.compile(r"[0-9a-f]{64}")


class Error(Exception):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def version_tuple(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", value):
        raise Error(f"Unsupported release version: {value!r}; expected major.minor.patch.")
    return tuple(map(int, value.split(".")))


def write_atomic(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise Error(f"Refusing to replace a symbolic link: {path}")
    fd, temporary = tempfile.mkstemp(prefix=".gitcomet-native-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def copy_binary(source: Path, destination: Path, expected_hash: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise Error(f"Refusing to replace a symbolic link: {destination}")
    fd, temporary = tempfile.mkstemp(prefix=".gitcomet-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as dst, source.open("rb") as src:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
            os.fchmod(dst.fileno(), 0o755)
        if file_digest(Path(temporary)) != expected_hash:
            raise Error("The bundled binary changed during installation; retry with an intact bundle.")
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def paths() -> tuple[Path, Path]:
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    if not data.is_absolute():
        raise Error("XDG_DATA_HOME must be an absolute path.")
    if any(char in str(data) for char in ("%", "\n", "\r")):
        raise Error("XDG_DATA_HOME containing % or a newline is not supported by this launcher.")
    base = data / "gitcomet-native-titlebar"
    for directory in (base, base / "bin", base / "backups"):
        if directory.is_symlink():
            raise Error(f"Refusing a symbolic link in the installation directory: {directory}")
    return base, data / "applications"


def runtime_dir(base: Path, state: dict | None, requested: Path | None = None) -> Path:
    directory = Path(requested if requested is not None else (state or {}).get("install_dir", base))
    if not directory.is_absolute() or any(char in str(directory) for char in ("%", "\n", "\r")):
        raise Error("The installation directory must be absolute and cannot contain % or newlines.")
    for path in (directory, directory / "bin"):
        if path.is_symlink():
            raise Error(f"Refusing a symbolic installation directory: {path}")
        if path.exists() and not path.is_dir():
            raise Error(f"Expected an installation directory, not a file: {path}")
    return directory


def bundle_info(bundle: Path | None = None) -> dict:
    info = json.loads(((bundle or BUNDLE) / "build-info.json").read_text(encoding="utf-8"))
    if not isinstance(info, dict) or info.get("schema") != 1:
        raise Error("Unsupported build-info.json format.")
    version_tuple(info.get("version"))
    if not isinstance(info.get("commit"), str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", info["commit"]):
        raise Error("build-info.json must identify the source commit.")
    if "target" in info and info["target"] != "x86_64-unknown-linux-gnu":
        raise Error("This installer supports the x86_64-unknown-linux-gnu bundle.")
    return info


def verified_binary(info: dict, directory: Path | None = None) -> tuple[Path, str]:
    binary = (directory or BUNDLE) / "bin/gitcomet"
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise Error(f"The bundle must contain an executable bin/gitcomet: {binary}")
    expected_hash = file_digest(binary)
    result = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=20, check=False)
    expected_version = "gitcomet " + info["version"]
    if result.returncode or result.stdout.strip().lower() != expected_version:
        raise Error(f"The binary failed its version check: {binary}\n" + result.stdout + result.stderr)
    if file_digest(binary) != expected_hash:
        raise Error("The bundled binary changed during its version check.")
    return binary, expected_hash


def desktop_fields(data: bytes) -> dict[str, str]:
    fields = {}
    section = ""
    for line in data.decode("utf-8").splitlines():
        if line.startswith("["):
            section = line.strip()
        elif section == "[Desktop Entry]" and "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            fields[key.strip()] = value
    return fields


def desktop_quote(value: str) -> str:
    # Desktop-entry string unescaping precedes Exec argument unescaping.
    escaped = value.replace("%", "%%")
    for old, new in (("\\", "\\\\\\\\"), ('"', '\\\\"'), ("`", "\\\\`"), ("$", "\\\\$")):
        escaped = escaped.replace(old, new)
    return '"' + escaped + '"'


def original_appimage(data: bytes) -> tuple[Path, str, str]:
    value = desktop_fields(data).get("Exec", "")
    # Preserve all arguments and field codes literally; never evaluate a shell.
    match = re.fullmatch(r'\s*("[^"\n]+"|[^\s"\n]+)(.*)', value)
    if not match:
        raise Error("Unsupported Exec entry in the GitComet launcher.")
    command = shlex.split(match.group(1))[0]
    image = re.fullmatch(r"gitcomet-v(\d+\.\d+\.\d+)-linux-x86_64(?:_[A-Za-z0-9-]+)?\.AppImage", Path(command).name)
    if not image:
        raise Error("The launcher must point directly to a GitComet Linux x86_64 AppImage.")
    if not Path(command).is_absolute():
        raise Error(f"The original AppImage needs an absolute path: {command}")
    return Path(command), image.group(1), match.group(2)


def patched_desktop(data: bytes, wrapper: Path, version: str, require_appimage: bool = True) -> bytes:
    appimage, original_version, suffix = original_appimage(data)
    if version_tuple(original_version) > version_tuple(version):
        raise Error(f"Refusing to replace the newer AppImage {original_version} with GitComet {version}.")
    if require_appimage and not appimage.is_file():
        raise Error(f"The original AppImage was not found: {appimage}")
    replacement = desktop_quote(str(wrapper)) + suffix
    add_try_exec = "TryExec" not in desktop_fields(data)
    result = []
    section = ""
    for line in data.decode("utf-8").splitlines(keepends=True):
        if line.startswith("["):
            section = line.strip()
        if section == "[Desktop Entry]":
            if line.startswith("Exec="):
                line = "Exec=" + replacement + "\n"
                if add_try_exec:
                    line += "TryExec=" + str(wrapper) + "\n"
            elif line.startswith("TryExec="):
                line = "TryExec=" + str(wrapper) + "\n"
            elif line.startswith("DBusActivatable="):
                line = "DBusActivatable=false\n"
        result.append(line)
    return "".join(result).encode("utf-8")


def without_appimagelauncher_actions(data: bytes, appimage: Path) -> bytes:
    """Remove only known helpers for this exact AppImage, preserving other actions."""
    action_ids = {"AppImageLauncher-Remove-AppImage": "remove", "AppImageLauncher-Update-AppImage": "update",
                  "Remove": "remove", "Update": "update"}
    sections = []
    for line in data.decode("utf-8").splitlines(keepends=True):
        if line.startswith("[") or not sections:
            sections.append((line.strip() if line.startswith("[") else "", []))
        sections[-1][1].append(line)
    removed = set()
    for name, lines in sections:
        action = name.removeprefix("[Desktop Action ").removesuffix("]")
        if action not in action_ids:
            continue
        fields = dict(line.rstrip("\r\n").split("=", 1) for line in lines if "=" in line and not line.startswith("#"))
        try:
            args = shlex.split(fields.get("Exec", ""))
        except ValueError:
            continue
        if not args or not Path(args[0]).is_absolute() or args[-1] != str(appimage):
            continue
        helper = Path(args[0])
        standard = len(args) == 2 and helper.parent.name == "appimagelauncher" and helper.name == action_ids[action]
        lite = (len(args) == 3 and helper.parent.name == "appimagelauncher-lite"
                and helper.name == "appimagelauncher-lite.AppImage" and args[1] == action_ids[action])
        if standard or lite:
            removed.add(action)
    if not removed:
        return data
    result = []
    for name, lines in sections:
        if name in {"[Desktop Action " + action + "]" for action in removed}:
            continue
        for line in lines:
            if name == "[Desktop Entry]" and line.startswith("Actions="):
                actions = [action for action in line.rstrip("\r\n").split("=", 1)[1].split(";") if action and action not in removed]
                line = "Actions=" + ";".join(actions) + (";" if actions else "") + "\n"
            result.append(line)
    return "".join(result).encode("utf-8")


def discover_launchers(appdir: Path, wrapper: Path, version: str, known: set[Path]) -> list[tuple[Path, bytes, bytes]]:
    main = appdir / "gitcomet.desktop"
    candidates = ([main] if main.exists() else []) + sorted(appdir.glob("appimagekit_*-GitComet.desktop"))
    found = []
    for path in candidates:
        if path in known:
            continue
        if path.is_symlink() or not path.is_file():
            raise Error(f"Expected a regular launcher file: {path}")
        original = path.read_bytes()
        found.append((path, original, patched_desktop(original, wrapper, version)))
    if not found and not known:
        raise Error(f"No GitComet AppImage launcher was found in {appdir}.")
    return found


def wrapper_bytes(base: Path) -> bytes:
    # Keep this compatible with the original 0.2.5 installer, which did not
    # store a wrapper hash. That installation is recognized by these exact bytes.
    return (
        "#!/bin/sh\n"
        "if [ -z \"${DISPLAY:-}\" ]; then\n"
        "  echo 'GitComet: XWayland DISPLAY is unavailable.' >&2\n"
        "  exit 1\n"
        "fi\n"
        "unset WAYLAND_DISPLAY WAYLAND_SOCKET\n"
        "export GITCOMET_NATIVE_TITLEBAR=1\n"
        "exec " + shlex.quote(str(base / "bin/gitcomet")) + ' "$@"\n'
    ).encode()


def load_state(base: Path) -> dict | None:
    path = base / "state.json"
    if path.is_symlink():
        raise Error(f"Refusing a symbolic state file: {path}")
    if not path.exists():
        return None
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("schema") != 1 or type(state.get("active")) is not bool:
        raise Error(f"Unsupported installation state: {path}")
    version_tuple(state.get("version"))
    if not SHA256.fullmatch(str(state.get("binary_sha256", ""))) or not isinstance(state.get("files"), list) or not state["files"]:
        raise Error(f"Incomplete installation state: {path}")
    if "wrapper_sha256" in state and not SHA256.fullmatch(str(state["wrapper_sha256"])):
        raise Error(f"Invalid wrapper checksum in {path}")
    seen = set()
    for record in state["files"]:
        if not isinstance(record, dict) or any(not isinstance(record.get(key), str) for key in ("path", "backup", "original_sha256", "installed_sha256")):
            raise Error(f"Invalid launcher record in {path}")
        if record["path"] in seen or not all(SHA256.fullmatch(record[key]) for key in ("original_sha256", "installed_sha256")):
            raise Error(f"Invalid launcher checksums or duplicate paths in {path}")
        if type(record.get("mode")) is not int or not 0 <= record["mode"] <= 0o7777:
            raise Error(f"Invalid launcher permissions in {path}")
        seen.add(record["path"])
    return state


def save_state(base: Path, state: dict) -> None:
    write_atomic(base / "state.json", (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode(), 0o600)


def checked_launchers(base: Path, appdir: Path, state: dict, restoring: bool = False) -> list[tuple[dict, bytes]]:
    checked = []
    for record in state["files"]:
        path, backup = Path(record["path"]), Path(record["backup"])
        if path.parent != appdir or backup.parent != base / "backups" or path.is_symlink() or backup.is_symlink():
            raise Error("Invalid path in the launcher backup.")
        original = backup.read_bytes()
        if digest(original) != record["original_sha256"]:
            raise Error(f"The original launcher backup has changed: {backup}")
        allowed = {record["installed_sha256"]}
        if restoring or state.get("installing"):
            allowed.add(record["original_sha256"])
            if "previous_installed_sha256" in record:
                allowed.add(record["previous_installed_sha256"])
        if not path.is_file() or file_digest(path) not in allowed:
            raise Error(f"A launcher has changed since installation; it will not be overwritten: {path}")
        checked.append((record, original))
    return checked


def checked_owned_files(base: Path, state: dict | None) -> None:
    for name, key, fallback in (
        ("bin/gitcomet", "binary_sha256", None),
        ("run", "wrapper_sha256", digest(wrapper_bytes(base))),
    ):
        path = base / name
        if path.is_symlink():
            raise Error(f"An installed file was replaced by a symbolic link: {path}")
        if state is None:
            if path.exists():
                raise Error(f"An untracked installation file already exists: {path}")
            continue
        allowed = {state.get(key, fallback)}
        if state.get("installing"):
            allowed.add(state.get("previous_" + key))
        actual = file_digest(path) if path.is_file() else None
        if actual not in allowed:
            raise Error(f"An installed file has changed; it will not be overwritten: {path}")


def checked_previous_runtime(base: Path, state: dict) -> list[Path]:
    """Validate the old files before cleanup; missing files allow interrupted cleanup."""
    previous = state.get("moved_from")
    if not previous:
        return []
    directory = runtime_dir(base, previous)
    if directory == runtime_dir(base, state):
        raise Error("Invalid move journal: source and destination are identical.")
    paths_to_remove = []
    for name, key in (("bin/gitcomet", "binary_sha256"), ("run", "wrapper_sha256")):
        path = directory / name
        if not SHA256.fullmatch(str(previous.get(key, ""))):
            raise Error("Invalid previous-runtime checksum in the move journal.")
        if path.is_symlink() or (path.exists() and (not path.is_file() or file_digest(path) != previous[key])):
            raise Error(f"A previous installation file has changed; it will not be removed: {path}")
        if path.is_file():
            paths_to_remove.append(path)
    return paths_to_remove


def references_appimage(data: bytes, appimage: Path) -> bool:
    # Check all sections, including Desktop Actions; unrelated actions stay intact.
    for line in data.decode("utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in ("Exec", "TryExec", "Icon"):
            unescaped = re.sub(r"\\([sntr\\])", lambda m: {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}.get(m[1], m[1]), value)
            if str(appimage) in value or str(appimage) in unescaped:
                return True
    return False


def removal_request(requested: Path | None, originals: list[bytes], updated: list[bytes]) -> tuple[Path, str | None] | None:
    if requested is None:
        return None
    appimage = Path(requested)
    if not appimage.is_absolute() or appimage.suffix != ".AppImage":
        raise Error("--remove-appimage requires one exact absolute .AppImage path.")
    if appimage not in {original_appimage(data)[0] for data in originals}:
        raise Error("The requested AppImage is not an original target in the managed launcher backups.")
    if appimage.is_symlink() or (appimage.exists() and not appimage.is_file()):
        raise Error(f"Only the explicitly supplied regular AppImage can be removed: {appimage}")
    if any(references_appimage(data, appimage) for data in updated):
        raise Error(f"A managed launcher still references {appimage} in Exec, TryExec, Icon or a Desktop Action; it cannot be removed.")
    return appimage, file_digest(appimage) if appimage.is_file() else None


def remove_requested_appimage(request: tuple[Path, str | None] | None, base: Path, appdir: Path, state: dict) -> None:
    if request is None:
        return
    appimage, expected_hash = request
    checked_owned_files(runtime_dir(base, state), state)
    checked_launchers(base, appdir, state)
    if any(references_appimage(Path(record["path"]).read_bytes(), appimage) for record in state["files"]):
        raise Error("A launcher still references the AppImage; it has not been removed.")
    if appimage.is_symlink() or (appimage.exists() and (not appimage.is_file() or file_digest(appimage) != expected_hash)):
        raise Error(f"The AppImage changed during installation; it has not been removed: {appimage}")
    if appimage.is_file():
        appimage.unlink()
        print(f"Removed the explicitly requested AppImage: {appimage}")
    else:
        print(f"The explicitly requested AppImage is already absent: {appimage}")


def refresh_desktop(appdir: Path) -> None:
    utility = shutil.which("update-desktop-database")
    if utility:
        subprocess.run([utility, str(appdir)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def apply(install_dir: Path | None = None, bundle: Path | None = None,
          remove_appimage: Path | None = None) -> None:
    base, appdir = paths()
    info = bundle_info(bundle)
    existing = load_state(base)
    active = existing is not None and existing["active"]
    current = runtime_dir(base, existing)
    destination = runtime_dir(base, existing, install_dir)
    moving = current != destination
    if existing and existing.get("installing") and moving:
        raise Error("Finish the interrupted installation at its recorded directory before moving it again.")
    if active and version_tuple(existing["version"]) > version_tuple(info["version"]):
        raise Error("Refusing to downgrade the active native-titlebar installation.")
    checked_owned_files(current, existing)
    if moving:
        checked_owned_files(destination, None)
    if existing:
        checked_previous_runtime(base, existing)
    retained = checked_launchers(base, appdir, existing) if active else []
    known = {Path(record["path"]) for record, _ in retained}
    additions = discover_launchers(appdir, destination / "run", info["version"], known)
    records, updates = [], []
    for old_record, original in retained:
        record = dict(old_record)
        modified = patched_desktop(original, destination / "run", info["version"], require_appimage=False)
        if record.get("appimage_actions_removed") or remove_appimage == original_appimage(original)[0]:
            cleaned = without_appimagelauncher_actions(modified, original_appimage(original)[0])
            if cleaned != modified:
                record["appimage_actions_removed"] = True
            modified = cleaned
        installed_hash = digest(modified)
        if installed_hash != record["installed_sha256"]:
            record.setdefault("previous_installed_sha256", record["installed_sha256"])
        record["installed_sha256"] = installed_hash
        records.append(record)
        updates.append((Path(record["path"]), modified, record["mode"]))
    for path, original, modified in additions:
        backup = base / "backups" / (digest(str(path).encode())[:16] + ".desktop")
        record = {"path": str(path), "backup": str(backup), "mode": stat.S_IMODE(path.stat().st_mode),
                  "original_sha256": digest(original), "installed_sha256": digest(modified)}
        if remove_appimage == original_appimage(original)[0]:
            cleaned = without_appimagelauncher_actions(modified, original_appimage(original)[0])
            if cleaned != modified:
                record["appimage_actions_removed"] = True
                record["installed_sha256"] = digest(cleaned)
            modified = cleaned
        records.append(record)
        updates.append((path, modified, record["mode"]))
    launcher_changes = any(record != old for record, (old, _) in zip(records, retained))
    removal = removal_request(remove_appimage,
                              [original for _, original in retained] + [original for _, original, _ in additions],
                              [modified for _, modified, _ in updates])
    if not os.environ.get("DISPLAY"):
        raise Error("XWayland DISPLAY is unavailable. Run this from a terminal in your GNOME desktop session.")
    binary, binary_hash = verified_binary(info, bundle)
    wrapper = wrapper_bytes(destination)
    if (active and not existing.get("installing") and not moving and not launcher_changes and not additions
            and existing["binary_sha256"] == binary_hash
            and existing.get("wrapper_sha256", digest(wrapper_bytes(current))) == digest(wrapper)):
        if removal:
            verified_binary(info, destination)
            remove_requested_appimage(removal, base, appdir, existing)
        print(f"GitComet {info['version']} with native titlebar is already installed. Close GitComet completely and reopen its launcher.")
        return
    # Recheck after the executable preflight, before any installation writes.
    checked_owned_files(current, existing)
    if moving:
        checked_owned_files(destination, None)
    if existing:
        checked_previous_runtime(base, existing)
    if active:
        checked_launchers(base, appdir, existing)
    for path, original, _ in additions:
        if path.is_symlink() or path.read_bytes() != original:
            raise Error(f"A launcher changed during the preflight: {path}")
    for (path, original, _), record in zip(additions, records[len(retained):]):
        write_atomic(Path(record["backup"]), original, 0o600)
    state = {"schema": 1, "version": info["version"], "commit": info["commit"], "active": True,
             "install_dir": str(destination),
             "binary_sha256": binary_hash, "wrapper_sha256": digest(wrapper), "files": records,
             "installing": True,
             "previous_binary_sha256": file_digest(destination / "bin/gitcomet") if (destination / "bin/gitcomet").is_file() else None,
             "previous_wrapper_sha256": file_digest(destination / "run") if (destination / "run").is_file() else None}
    if existing and existing.get("moved_from"):
        state["moved_from"] = existing["moved_from"]
    elif moving and existing:
        state["moved_from"] = {"install_dir": str(current),
                               "binary_sha256": existing["binary_sha256"],
                               "wrapper_sha256": existing.get("wrapper_sha256", digest(wrapper_bytes(current)))}
    # Journal both runtime locations and launcher hashes before replacing files.
    # Retrying a partial move recognizes the old and new launcher bytes.
    save_state(base, state)
    copy_binary(binary, destination / "bin/gitcomet", binary_hash)
    write_atomic(destination / "run", wrapper, 0o755)
    if verified_binary(info, destination)[1] != binary_hash:
        raise Error("The installed binary changed before launcher activation.")
    for path, modified, mode in updates:
        write_atomic(path, modified, mode)
    checked_owned_files(destination, {**state, "installing": False})
    for path in checked_previous_runtime(base, state):
        path.unlink()
    for record in state["files"]:
        record.pop("previous_installed_sha256", None)
    for key in ("installing", "previous_binary_sha256", "previous_wrapper_sha256", "moved_from"):
        state.pop(key, None)
    save_state(base, state)
    refresh_desktop(appdir)
    remove_requested_appimage(removal, base, appdir, state)
    print(f"Installed GitComet {info['version']}. Close GitComet completely and reopen it through its existing launcher.")
    print(f"Terminal launcher: {shlex.quote(str(destination / 'run'))}")
    print("Original launcher backups are retained. --restore requires the original AppImage files to exist.")


def restore() -> None:
    base, appdir = paths()
    state = load_state(base)
    if not state or not state["active"]:
        print("There is no active launcher change to restore.")
        return
    checked = checked_launchers(base, appdir, state, restoring=True)
    for _, original in checked:
        appimage = original_appimage(original)[0]
        if not appimage.is_file():
            raise Error(f"Cannot restore: the original AppImage is missing: {appimage}. Restore that file first; launchers were not changed.")
    for record, original in checked:
        write_atomic(Path(record["path"]), original, record["mode"])
    state["active"] = False
    save_state(base, state)
    refresh_desktop(appdir)
    print("Original AppImage launchers restored. Close GitComet completely and reopen it.")


def status() -> None:
    base, appdir = paths()
    state = load_state(base)
    if not state or not state["active"]:
        print("Native-titlebar launchers are not active.")
        return
    directory = runtime_dir(base, state)
    checked_owned_files(directory, state)
    checked_previous_runtime(base, state)
    checked_launchers(base, appdir, state)
    if state.get("installing"):
        print("An installation was interrupted. Rerun install.py to finish, or use --restore.")
    else:
        print(f"GitComet {state['version']} native-titlebar launchers are active; installed file checksums match.")
    print(f"Launcher: {directory / 'run'}")
    print(f"Original launchers backed up: {len(state['files'])}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Install a bundled GitComet native-titlebar build on Linux x86_64.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--apply", action="store_true", help="install or upgrade the bundled build (default)")
    modes.add_argument("--restore", action="store_true", help="restore the original AppImage launchers")
    modes.add_argument("--status", action="store_true", help="check the active installation without changing it")
    parser.add_argument("--bundle", type=Path, help="directory containing build-info.json and bin/gitcomet")
    parser.add_argument("--install-dir", type=Path, help="absolute runtime directory; later upgrades remember it")
    parser.add_argument("--remove-appimage", type=Path, help="remove exactly this managed original .AppImage after successful installation")
    args = parser.parse_args()
    if (args.restore or args.status) and (args.bundle or args.install_dir or args.remove_appimage):
        parser.error("--bundle, --install-dir and --remove-appimage are installation options")
    if os.geteuid() == 0:
        raise Error("Run this installer without sudo, as your desktop user.")
    if sys.platform != "linux" or os.uname().machine != "x86_64":
        raise Error("This bundle is for Linux x86_64.")
    if args.restore:
        restore()
    elif args.status:
        status()
    else:
        apply(install_dir=args.install_dir, bundle=args.bundle, remove_appimage=args.remove_appimage)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Error, OSError, UnicodeError, ValueError, subprocess.SubprocessError) as exc:
        print("Error: " + str(exc), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted. Rerun to finish, or use --restore to recover the AppImage launchers.", file=sys.stderr)
        sys.exit(130)
