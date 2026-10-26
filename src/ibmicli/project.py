"""Repository project: one explicitly selected ibmiproj.toml at the root.

    repository/ibmiproj.toml
    repository/MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle

[apps.MYAPP] declares the app folder and its env and objlib. The next two
folders identify the source library and source file. An app with srclib has
no library folders: its source file folders sit directly below it.

    repository/MYAPP/QRPGLESRC/HELLO.sqlrpgle     [apps.MYAPP] srclib = "MYAPP_SRC"
 Global [libraries] and
[srcfile] mappings can be overridden per app. Repository tasks override global
tasks with the same name. Credentials remain in global configuration.

The CLI never searches for this file: callers pass -proj FILE. Only declared
app folders participate in resolution. Apps without env support reading sources.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import tasks as tasks_mod
from .config import Config, ConfigError, Env
from .naming import InvalidName, check_object
from .tasks import Task

PROJECT_FILE = "ibmiproj.toml"

# Default job libraries are not valid compilation destinations:
# an unset current library must not cause accidental builds in QGPL.
NO_CURLIB = {"", "QGPL", "*CRTDFT"}

# Allowed app keys; unknown keys are errors.
APP_KEYS = ("env", "objlib", "srclib", "libraries", "srcfile")


@dataclass
class App:
    """A repository application: [apps.NAME], folder NAME."""

    name: str  # Spelling in TOML.
    root: Path  # ROOT/NAME, using on-disk case.
    path: Path  # Project file, for diagnostics.
    env: str = ""  # Empty means no environment: read-only app.
    objlib: str = ""
    # The only source library of a two-level app (APP/SOURCEFILE/MEMBER).
    # Empty means one folder per library (APP/LIBRARY/SOURCEFILE/MEMBER).
    srclib: str = ""
    # Mapping keys as written in TOML, so new folders keep that spelling.
    names: dict[str, str] = field(default_factory=dict)  # FOLDER -> Folder
    # App mappings override global mappings.
    libraries: dict[str, str] = field(default_factory=dict)  # FOLDER -> LIBRARY
    srcfiles: dict[str, str] = field(default_factory=dict)  # FOLDER -> FILE

    @property
    def where(self) -> str:
        """Location of app values, for diagnostics."""
        return f"[apps.{self.name}] in {self.path}"


@dataclass
class Project:
    """Project file contents and path."""

    path: Path
    apps: dict[str, App] = field(default_factory=dict)
    libraries: dict[str, str] = field(default_factory=dict)  # FOLDER -> LIBRARY
    srcfiles: dict[str, str] = field(default_factory=dict)  # FOLDER -> FILE
    tasks: dict[str, Task] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)  # FOLDER -> Folder

    @property
    def root(self) -> Path:
        return self.path.parent

    def app(self, name: str) -> App | None:
        """Find an app by name, ignoring case."""
        wanted = name.upper()
        for app in self.apps.values():
            if app.name.upper() == wanted:
                return app
        return None

    def app_of(self, target: Path) -> App | None:
        """App containing target, identified by the first folder below the root."""
        parts = _parts(self.root, target)
        return self.app(parts[0]) if parts else None

    # App-specific mappings override global mappings
    # for the same folder.

    def libraries_of(self, app: App) -> dict[str, str]:
        return {**self.libraries, **app.libraries}

    def srcfiles_of(self, app: App) -> dict[str, str]:
        return {**self.srcfiles, **app.srcfiles}

    def library_of(self, app: App, folder: str) -> str:
        """Library for an app folder: its name unless mapped."""
        return self.libraries_of(app).get(folder.upper(), folder.upper())

    def srcfile_of(self, app: App, folder: str) -> str:
        """Source file for an app folder: its name unless mapped."""
        return self.srcfiles_of(app).get(folder.upper(), folder.upper())

    def library_folder(self, app: App, library: str) -> str | None:
        """Inverse of library_of, whether the folder exists or not.

        None means the folder with that name maps to a different library.
        """
        return _inverse(self.libraries_of(app), library, app.where, "library")

    def folder_of(self, app: App, srcfile: str) -> str | None:
        """Source file folder, the inverse of srcfile_of."""
        return _inverse(self.srcfiles_of(app), srcfile, app.where, "source file")

    def has_library(self, app: App, library: str) -> bool:
        """Whether the library folder exists inside the app.

        A srclib app holds that library alone, in the app folder itself. It
        is declared outright, so it owns the library before the folder exists:
        pull creates it there.
        """
        if app.srclib:
            return library.upper() == app.srclib
        folder = self.library_folder(app, library)
        return folder is not None and _existing(app.root, folder) is not None

    def spelling(self, app: App, folder: str) -> str:
        """A mapped folder name as written in TOML; other names unchanged."""
        return app.names.get(folder) or self.names.get(folder) or folder


def _inverse(mapping: dict[str, str], value: str, where: str, what: str) -> str | None:
    wanted = value.upper()
    named = sorted(key for key, mapped in mapping.items() if mapped == wanted)
    if len(named) > 1:
        raise ConfigError(
            f"in {where} {what} {wanted} maps to multiple folders ({', '.join(named)})"
        )
    if named:
        return named[0]
    if mapping.get(wanted, wanted) != wanted:
        return None  # The folder with this name maps elsewhere.
    return wanted


@dataclass
class Target:
    """Local file resolved through the project."""

    project: Project
    app: App
    env: Env | None
    library: str
    srcfile: str


# --- Reading -----------------------------------------------------------------

_read_cache: dict[Path, tuple[int, Project]] = {}


def load(path: Path | str | None) -> Project | None:
    """Project selected by -proj, or None. Only file paths are accepted."""
    if path is None:
        return None
    path = Path(path).expanduser()
    if path.is_dir():
        raise ConfigError(
            f"-proj requires a file, not a directory: -proj {path / PROJECT_FILE}"
        )
    if not path.is_file():
        raise ConfigError(f"-proj {path}: file not found")
    return read(path.resolve())


def read(path: Path) -> Project:
    """Cache parsed projects until their modification time changes."""
    try:
        stamp = path.stat().st_mtime_ns
    except OSError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    cached = _read_cache.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]

    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    if {"env", "objlib", "srclib"} & set(raw):
        # App settings at the root do not define a repository project.
        # An explicit app table is required.
        raise ConfigError(
            f"{path}: app settings must be inside [apps.NAME]\n"
            f"       use one {PROJECT_FILE} at the repository root; "
            f"source libraries come from the folder path"
        )

    project = Project(
        path=path,
        libraries=_mapping(raw.get("libraries")),
        srcfiles=_mapping(raw.get("srcfile")),
        tasks=tasks_mod.read(raw.get("tasks")),
        names=_names(raw.get("libraries"), raw.get("srcfile")),
    )
    apps = raw.get("apps") or {}
    if not isinstance(apps, dict):
        raise ConfigError(f"{path}: 'apps' must be a table: [apps.NAME]")
    for name, values in apps.items():
        if not isinstance(values, dict):
            raise ConfigError(f"{path}: [apps.{name}] must be a table")
        name = str(name).strip()
        if project.app(name) is not None:
            raise ConfigError(f"{path}: duplicate app {name}")
        # Unknown app keys must fail: silently ignoring a misspelled mapping
        # could compile sources from the wrong folder.
        unknown = sorted(str(key) for key in values if key not in APP_KEYS)
        if unknown:
            raise ConfigError(
                f"{path}: in [apps.{name}] unknown keys: {', '.join(unknown)}\n"
                f"       allowed keys: {', '.join(APP_KEYS)}"
            )
        project.apps[name] = App(
            name=name,
            root=_child(path.parent, name),
            path=path,
            env=str(values.get("env") or "").strip(),
            objlib=_objlib(values.get("objlib"), f"objlib of [apps.{name}] in {path}"),
            srclib=_objlib(values.get("srclib"), f"srclib of [apps.{name}] in {path}"),
            libraries=_app_mapping(values, "libraries", name, path),
            srcfiles=_app_mapping(values, "srcfile", name, path),
            names=_names(values.get("libraries"), values.get("srcfile")),
        )
        app = project.apps[name]
        if app.srclib and app.libraries:
            raise ConfigError(
                f"{path}: [apps.{name}] has srclib, so it has no library folders\n"
                f"       remove [apps.{name}.libraries]"
            )
    _read_cache[path] = (stamp, project)
    return project


def _objlib(value: object, what: str) -> str:
    objlib = str(value or "").strip()
    if not objlib:
        return ""
    try:
        return check_object(objlib, what)
    except InvalidName as exc:
        raise ConfigError(str(exc)) from exc


def _app_mapping(values: dict, key: str, name: str, path: Path) -> dict[str, str]:
    raw = values.get(key)
    if raw is not None and not isinstance(raw, dict):
        raise ConfigError(f"{path}: '{key}' of [apps.{name}] must be a table: [apps.{name}.{key}]")
    return _mapping(raw)


def _names(*tables: object) -> dict[str, str]:
    return {
        str(key).strip().strip("/").upper(): str(key).strip().strip("/")
        for table in tables if isinstance(table, dict)
        for key in table
    }


def _mapping(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {
        str(key).strip().strip("/").upper(): str(value).strip().upper()
        for key, value in raw.items()
        if str(value).strip()
    }


def forget() -> None:
    """Clear cached projects after set-project in the interactive shell."""
    _read_cache.clear()


def _folder_of(path: Path) -> Path:
    """Folder for a path; use the extension when the path does not exist."""
    if path.is_dir():
        return path
    if path.is_file() or path.suffix:
        return path.parent
    return path


def _parts(root: Path, target: Path) -> tuple[str, ...]:
    folder = _folder_of(target.resolve())
    try:
        return folder.relative_to(root).parts
    except ValueError:
        return ()


# --- Local file to member -----------------------------------------------------


def resolve(cfg: Config, project: Project | None, target: Path) -> Target | None:
    """Resolve environment, library and source file from an explicit project.

    Without a project, outside declared apps, or above LIBRARY/SOURCEFILE,
    return None so the caller requests explicit values. An app naming an
    unknown environment raises ConfigError rather than using another one.
    """
    if project is None:
        return None
    app = project.app_of(target)
    if app is None:
        return None
    parts = _parts(app.root, target)
    if app.srclib:
        if not parts:
            return None
        library = app.srclib
        srcfile = check_object(project.srcfile_of(app, parts[0]), "source file")
    else:
        if len(parts) < 2:
            return None
        library = check_object(project.library_of(app, parts[0]), "library")
        srcfile = check_object(project.srcfile_of(app, parts[1]), "source file")
    env = cfg.named_env(app.env) if app.env else None
    return Target(project=project, app=app, env=env, library=library, srcfile=srcfile)


def library_at(project: Project, target: Path) -> str:
    """Resolve a library folder before a source file is available; empty if unknown."""
    app = project.app_of(target)
    if app is not None and app.srclib:
        return app.srclib
    parts = _parts(app.root, target) if app is not None else ()
    if not parts:
        return ""
    try:
        return check_object(project.library_of(app, parts[0]), "library")
    except InvalidName:
        return ""


def env_of(cfg: Config, app: App | None) -> Env | None:
    """App environment, or None if unavailable."""
    return cfg.envs.get(app.env) if app is not None and app.env else None


def object_library(app: App | None, env: Env | None) -> str:
    """App object library, falling back to the environment current library.

    Return an empty string if neither is a safe compilation destination.
    The caller reports the missing value through missing_library.
    """
    if app is not None and app.objlib:
        return app.objlib
    if env is not None and env.curlib not in NO_CURLIB:
        return env.curlib
    return ""


def missing_library(app: App | None, env: Env | None) -> ConfigError:
    """Explain the missing object library and how to supply it."""
    where = f"in {app.where} " if app is not None else ""
    if env is None:
        return ConfigError(
            f"object library is unknown: {where}set 'objlib', "
            f"or select an environment (ibmicli env use NAME)"
        )
    cur = env.curlib or "none"
    return ConfigError(
        f"object library is unknown: environment '{env.name}' has current "
        f"library {cur}\n"
        f"       {where}set 'objlib', or refresh the environment: "
        f"ibmicli env refresh {env.name}"
    )


def source_library(target: Target) -> str:
    """Source library of a project file, from its library folder."""
    return target.library


# Keep the optional -proj shortcut after
# the explicit values required to fix the error.
PROJ_HINT = f"\n       (for a file in an app declared in {PROJECT_FILE}, use -proj FILE)"


def no_project(instead: str = "") -> ConfigError:
    """Explain missing -proj, or put it last as a shortcut to explicit values."""
    if instead:
        return ConfigError(f"{instead}{PROJ_HINT}")
    return ConfigError(f"a project is required: supply -proj ROOT/{PROJECT_FILE}")


# --- Library to folder -------------------------------------------------------


def folder_for(
    project: Project, library: str, srcfile: str, start: Path | None = None
) -> Path | None:
    """Find LIBRARY/SRCFILE among declared apps that have its library folder.

    This reverses resolve for reads. With multiple owners, prefer the app
    containing start; otherwise refuse the ambiguous destination. None means
    no owner: copy resolution can place it in the source app using place.
    """
    lib = library.upper()
    owners = [app for app in project.apps.values() if project.has_library(app, lib)]
    if not owners:
        return None
    if len(owners) > 1:
        current = project.app_of(start) if start is not None else None
        mine = [app for app in owners if current is not None and app.name == current.name]
        if not mine:
            raise ConfigError(
                f"library {lib} belongs to multiple apps in {project.path} "
                f"({', '.join(app.name for app in owners)})\n"
                f"       choose the download folder with: -o FOLDER"
            )
        owners = mine
    return place(project, owners[0], lib, srcfile)


def place(project: Project, app: App, library: str, srcfile: str) -> Path | None:
    """Existing or prospective LIBRARY/SRCFILE location inside app.

    Return None if a mapping assigns either folder name to another target,
    or if app has srclib and library is a different one: a two-level app has
    no place for other libraries.
    """
    src_folder = project.folder_of(app, srcfile)
    if src_folder is None:
        return None
    src_name = project.spelling(app, src_folder)
    if app.srclib:
        if library.upper() != app.srclib:
            return None
        return _child(app.root, src_name)
    lib_folder = project.library_folder(app, library)
    if lib_folder is None:
        return None
    return _child(_child(app.root, project.spelling(app, lib_folder)), src_name)


def _existing(parent: Path, name: str) -> Path | None:
    """Existing subfolder with its actual on-disk spelling."""
    try:
        for entry in parent.iterdir():
            if entry.is_dir() and entry.name.upper() == name.upper():
                return entry
    except OSError:
        pass
    return None


def _child(parent: Path, name: str) -> Path:
    """Subfolder with existing on-disk case, if available.

    This avoids creating QRPGLESRC beside qrpglesrc on case-sensitive disks.
    """
    return _existing(parent, name) or parent / name


# --- Tasks -------------------------------------------------------------------


def tasks_for(cfg: Config, project: Project | None) -> tuple[dict[str, Task], dict[str, Path]]:
    """Available tasks and their files; project tasks override global names."""
    found = dict(cfg.tasks)
    where = {name: cfg.path for name in cfg.tasks}
    if project is not None:
        for name, task in project.tasks.items():
            found[name] = task
            where[name] = project.path
    return found, where
