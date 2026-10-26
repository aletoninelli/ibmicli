"""Locate VS Code, build the bundled extension and install it.

The extension is handwritten JavaScript bundled with the Python package.
Building a VSIX needs only zipfile and XML metadata, so installation needs
neither npm nor a JavaScript compilation toolchain.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from . import __version__, output

# Extension sources bundled in the Python package.
SOURCE = Path(__file__).parent / "extension"

PUBLISHER = "ibmicli"
NAME = "ibmicli"
IDENTIFIER = f"{PUBLISHER}.{NAME}"

# Task menu command whose shortcut comes
# from global config.toml.
TASK_COMMAND = "ibmicli.runTask"

# Known code executable locations for installations
# where the command has not been added to PATH.
CANDIDATES = (
    Path("/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"),
    Path.home() / "Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
    Path("/usr/local/bin/code"),
    Path("/opt/homebrew/bin/code"),
)


@dataclass(frozen=True)
class Companion:
    """Third-party extension and its installation purpose."""

    identifier: str
    why: str


# Language support supplies source-language names and syntax coloring.
# These capabilities need no IBM i connection.
COMPANIONS = (
    Companion("barrettotte.ibmi-languages", "syntax coloring for RPGLE, CLLE, DDS, CL"),
    # IBM's official debugger consumes launch settings prepared by ibmicli.
    # It does not require Code for IBM i.
    Companion("IBM.ibmidebug", "batch debug and service entry points (IBM i Debug)"),
)

# Optional companions installed with -extra.
# Code for IBM i uses a direct remote workflow, so it is not
# required for local files and FTP deploys.
EXTRA = (
    Companion("halcyontechltd.vscode-rpgle", "RPGLE linter and tools"),
    Companion("halcyontechltd.code-for-ibmi", "browse IBM i libraries and members in VS Code"),
)


class VSCodeError(Exception):
    """VS Code unavailable or a code command failed."""


def code_command() -> Path:
    """Find code or explain how to install it."""
    found = shutil.which("code")
    if found:
        return Path(found)
    for candidate in CANDIDATES:
        if candidate.is_file():
            return candidate
    raise VSCodeError(
        "Visual Studio Code not found\n"
        "       install it from https://code.visualstudio.com/\n"
        "       if installed elsewhere, add the 'code' command to PATH"
    )


def find_code() -> Path | None:
    """Find code without raising an error, for callers with a fallback."""
    try:
        return code_command()
    except VSCodeError:
        return None


def _run(code: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [str(code), *arguments], capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise VSCodeError(f"cannot run {code}: {exc}") from exc


def installed(code: Path) -> dict[str, str]:
    """Installed extension IDs and versions."""
    result = _run(code, "--list-extensions", "--show-versions")
    if result.returncode != 0:
        raise VSCodeError(f"'code --list-extensions' failed: {result.stderr.strip()}")
    found: dict[str, str] = {}
    for line in result.stdout.splitlines():
        identifier, _, version = line.strip().partition("@")
        if identifier:
            found[identifier.lower()] = version
    return found


def install_extension(code: Path, identifier: str) -> None:
    """Install a marketplace extension."""
    result = _run(code, "--install-extension", identifier, "--force")
    if result.returncode != 0:
        raise VSCodeError(
            f"could not install {identifier}\n"
            f"       {result.stderr.strip() or result.stdout.strip()}"
        )


def install_package(code: Path, package: Path) -> None:
    """Install a locally built VSIX."""
    result = _run(code, "--install-extension", str(package), "--force")
    if result.returncode != 0:
        raise VSCodeError(
            f"could not install {package.name}\n"
            f"       {result.stderr.strip() or result.stdout.strip()}"
        )


def uninstall_extension(code: Path, identifier: str) -> None:
    result = _run(code, "--uninstall-extension", identifier)
    if result.returncode != 0:
        raise VSCodeError(
            f"could not uninstall {identifier}\n"
            f"       {result.stderr.strip() or result.stdout.strip()}"
        )


def build(destination: Path, tasks_key: str) -> Path:
    """Build a VSIX containing its manifest, OPC content types and extension files."""
    if not (SOURCE / "package.json").is_file():
        raise VSCodeError(
            f"extension sources not found at {SOURCE}\n"
            f"       reinstall ibmicli: ./deploy-local"
        )

    package = destination / f"{NAME}-{__version__}.vsix"
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("extension.vsixmanifest", _manifest())
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        for path in sorted(SOURCE.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            name = f"extension/{path.relative_to(SOURCE).as_posix()}"
            if path.name == "package.json":
                archive.writestr(name, _package_json(tasks_key))
            else:
                archive.write(path, name)
    return package


def _package_json(tasks_key: str) -> str:
    """Extension manifest with the CLI version and configured task shortcut.

    Sharing one version prevents an old extension from calling a newer CLI.
    tasks_key lives only in user configuration and is embedded during build,
    so changing it requires reinstalling the extension.
    """
    text = (SOURCE / "package.json").read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VSCodeError(f"{SOURCE / 'package.json'}: invalid JSON: {exc}") from exc

    data["version"] = __version__
    bindings = data.get("contributes", {}).get("keybindings", [])
    found = [b for b in bindings if b.get("command") == TASK_COMMAND]
    if not found:
        raise VSCodeError(
            f"{SOURCE / 'package.json'}: missing keybinding for {TASK_COMMAND}"
        )
    for binding in found:
        binding["key"] = tasks_key
        # Use the configured shortcut consistently on all platforms.
        binding.pop("mac", None)
        binding.pop("linux", None)
        binding.pop("win", None)
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _manifest() -> str:
    return MANIFEST % {
        "publisher": PUBLISHER,
        "name": NAME,
        "version": __version__,
        "description": _description(),
    }


def _description() -> str:
    """Use package.json as the single description source for both extension manifests."""
    text = (SOURCE / "package.json").read_text(encoding="utf-8")
    found = re.search(r'"description"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    return found.group(1) if found else "ibmicli support for IBM i sources."


MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0"
    xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011"
    xmlns:d="http://schemas.microsoft.com/developer/vsx-schema-design/2011">
  <Metadata>
    <Identity Language="en-US" Id="%(name)s" Version="%(version)s" Publisher="%(publisher)s" />
    <DisplayName>ibmicli</DisplayName>
    <Description xml:space="preserve">%(description)s</Description>
    <Tags>rpgle,ibmi,as400,copy</Tags>
    <Categories>Programming Languages</Categories>
    <GalleryFlags>Public</GalleryFlags>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value="^1.70.0" />
      <Property Id="Microsoft.VisualStudio.Code.ExtensionDependencies" Value="" />
      <Property Id="Microsoft.VisualStudio.Code.ExtensionPack" Value="" />
      <Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value="workspace,ui" />
    </Properties>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code" />
  </Installation>
  <Dependencies />
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json"
        Addressable="true" />
    <Asset Type="Microsoft.VisualStudio.Services.Content.Details" Path="extension/README.md"
        Addressable="true" />
  </Assets>
</PackageManifest>
"""

CONTENT_TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="vsixmanifest" ContentType="text/xml" />
  <Default Extension="json" ContentType="application/json" />
  <Default Extension="js" ContentType="application/javascript" />
  <Default Extension="md" ContentType="text/markdown" />
  <Default Extension="txt" ContentType="text/plain" />
  <Default Extension="png" ContentType="image/png" />
</Types>
"""


# Read installed extension manifests
# to detect other bindings for the task shortcut.
EXTENSIONS_DIR = Path.home() / ".vscode" / "extensions"

# User keybindings can already disable a conflicting command.
# Suppress resolved conflicts rather than repeating
# warnings that no longer need action.
USER_KEYBINDINGS = (
    Path.home() / "Library/Application Support/Code/User/keybindings.json",  # macOS
    Path.home() / ".config/Code/User/keybindings.json",                      # Linux
)


def _disabled(tasks_key: str) -> set[str]:
    """Commands explicitly disabled for this shortcut in user keybindings.

    Strip comment-only JSON lines without touching // inside strings.
    If parsing fails, keep potential conflicts visible.
    """
    removed: set[str] = set()
    wanted = tasks_key.strip().lower()
    for path in USER_KEYBINDINGS:
        if not path.is_file():
            continue
        try:
            text = re.sub(r"^\s*//.*$", "", path.read_text(encoding="utf-8"), flags=re.M)
            for binding in json.loads(text):
                command = str(binding.get("command", ""))
                if command.startswith("-") and str(binding.get("key", "")).lower() == wanted:
                    removed.add(command[1:])
        except (OSError, ValueError, TypeError):
            continue
    return removed


def conflicts(tasks_key: str) -> list[tuple[str, str]]:
    """Other extensions binding the same shortcut while editing.

    When bindings compete, the extension loaded last wins, making the task
    menu appear broken without an error. Report actionable conflicts.
    """
    wanted = tasks_key.strip().lower()
    disabled = _disabled(tasks_key)
    found: list[tuple[str, str]] = []
    try:
        folders = sorted(EXTENSIONS_DIR.iterdir())
    except OSError:
        return found

    for folder in folders:
        manifest = folder / "package.json"
        if not manifest.is_file() or folder.name.startswith(IDENTIFIER):
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for binding in data.get("contributes", {}).get("keybindings", []) or []:
            keys = {str(binding.get(k, "")).strip().lower() for k in ("key", "mac")}
            command = str(binding.get("command", "?"))
            if wanted in keys and command not in disabled:
                found.append((folder.name, command))
    return found


def _report_conflicts(tasks_key: str) -> None:
    """Report competing shortcuts and how to resolve them."""
    others = conflicts(tasks_key)
    if not others:
        return
    output.fail(f"{tasks_key.upper()} is also bound by another extension:")
    for name, command in others:
        output.note(f"{name}  ->  {command}")
    output.note(
        "the extension loaded last wins, so the shortcut may "
        "run the other command.\n"
        "Change 'tasks_key' in configuration and rerun "
        "'ibmicli vscode install',\n"
        "or disable the other binding in VS Code keybindings.json:\n"
        f'  {{ "key": "{tasks_key}", "command": "-{others[0][1]}", "when": "editorTextFocus" }}'
    )


def install(tasks_key: str, extra: bool = False, only_ours: bool = False) -> int:
    """Install ibmicli and required companion extensions."""
    code = code_command()
    output.info(f"VS Code: {code}")

    with TemporaryDirectory(prefix="ibmicli-vsix-") as directory:
        package = build(Path(directory), tasks_key)
        install_package(code, package)
    output.ok(f"{IDENTIFIER} {__version__}  (ibmicli extension)")
    output.info(f"        task menu: {tasks_key.upper()} on the open source")
    _report_conflicts(tasks_key)

    if only_ours:
        return 0

    failures = 0
    present = installed(code)
    wanted = [*COMPANIONS, *(EXTRA if extra else ())]
    for companion in wanted:
        if companion.identifier.lower() in present:
            output.skip(f"{companion.identifier}  (already installed)")
            continue
        try:
            install_extension(code, companion.identifier)
        except VSCodeError as exc:
            output.fail(f"{companion.identifier}  ({companion.why})")
            output.note(str(exc))
            failures += 1
            continue
        output.ok(f"{companion.identifier}  ({companion.why})")

    if not extra:
        output.info("")
        output.info("Optional extensions, with 'ibmicli vscode install -extra':")
        for companion in EXTRA:
            output.info(f"  {companion.identifier:<36} {companion.why}")

    output.info("")
    output.info(
        "Reload VS Code ('Developer: Reload Window'): "
        "the old extension stays in memory until the window reloads."
    )
    return 1 if failures else 0


def uninstall() -> int:
    code = code_command()
    uninstall_extension(code, IDENTIFIER)
    output.ok(f"{IDENTIFIER} removed")
    output.info("Third-party extensions remain installed; remove them from VS Code if unused.")
    return 0


def status(tasks_key: str) -> int:
    """Inspect installed extensions without changing them."""
    code = find_code()
    if code is None:
        output.error("Visual Studio Code not found")
        output.info("install it from https://code.visualstudio.com/, then: ibmicli vscode install")
        return 1

    print(f"VS Code:   {code}")
    present = installed(code)

    version = present.get(IDENTIFIER.lower())
    if version is None:
        print("extension: NOT installed  (run 'ibmicli vscode install')")
    elif version != __version__:
        print(f"extension: {version}, CLI version: {__version__}")
        print("            update it with 'ibmicli vscode install'")
    else:
        print(f"extension: {version}")

    print(f"shortcut: {tasks_key.upper()}  (task menu; change with 'tasks_key')")
    _report_conflicts(tasks_key)
    print()
    print("companion extensions:")
    for companion in (*COMPANIONS, *EXTRA):
        mark = present.get(companion.identifier.lower())
        state = mark if mark else "-"
        print(f"  {companion.identifier:<36} {state:<10} {companion.why}")
    return 0
