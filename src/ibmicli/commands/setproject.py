"""Create or update an app in the explicit repository project file.

Preserve other apps, folder mappings and tasks. Rewriting the file replaces
handwritten comments with generated documentation. VS Code configuration
belongs to vscode install.
"""

from __future__ import annotations

from pathlib import Path

import typer

from .. import output, project as prj
from ..config import Config, ConfigError, toml_str, task_lines
from ..naming import InvalidName, check_object
from ..project import PROJECT_FILE, App, Project

# Answer that clears an optional value.
NONE = "-"


def run(
    cfg: Config,
    name: str,
    proj: Path | None,
    env: str | None = None,
    objlib: str | None = None,
    srclib: str | None = None,
) -> int:
    if proj is None:
        raise ConfigError(
            f"supply the repository file: -proj ROOT/{PROJECT_FILE} "
            f"(created if missing)"
        )
    path = proj.expanduser()
    if path.is_dir():
        raise ConfigError(f"-proj requires a file, not a directory: -proj {path / PROJECT_FILE}")
    if not path.parent.is_dir():
        raise ConfigError(f"-proj {path}: directory {path.parent} does not exist")
    path = path.resolve()
    name = _app_name(name)

    project = prj.read(path) if path.is_file() else Project(path=path)
    old = project.app(name)
    asked = env is None and objlib is None and srclib is None
    if old is not None and asked:
        typer.echo(f"[apps.{old.name}] already exists in {path}: Enter keeps the current value.")

    try:
        if asked:
            env, objlib = _ask(cfg, old)
        else:
            env = _env(cfg, env if env is not None else (old.env if old else ""))
            objlib = _library(objlib if objlib is not None else (old.objlib if old else ""))
        srclib = _library(srclib if srclib is not None else (old.srclib if old else ""))
    except InvalidName as exc:
        raise ConfigError(str(exc)) from exc

    key = old.name if old is not None else name
    app = App(
        name=key, root=prj._child(path.parent, key), path=path, env=env, objlib=objlib,
        srclib=srclib,
        libraries=dict(old.libraries) if old else {},
        srcfiles=dict(old.srcfiles) if old else {},
        names=dict(old.names) if old else {},
    )
    if app.srclib and app.libraries:
        raise ConfigError(
            f"[apps.{key}] has [apps.{key}.libraries]: an app with srclib has no "
            f"library folders\n       remove that table from {path} first"
        )
    project.apps[key] = app
    path.write_text(text(project), encoding="utf-8")
    prj.forget()
    output.ok(f"{path}  [apps.{key}]")
    _summary(cfg, project, app)
    return 0


def _app_name(name: str) -> str:
    """An app name is one folder below the repository root, not a path."""
    name = name.strip().strip("/")
    if not name or "/" in name or name in (".", ".."):
        raise ConfigError(
            f"'{name}': app must be a folder name below the repository root, e.g. MYAPP"
        )
    return name


def _ask(cfg: Config, old: App | None) -> tuple[str, str]:
    if cfg.envs:
        typer.echo(f"Environments: {', '.join(sorted(cfg.envs))}   ('{NONE}' = none)")
    else:
        typer.echo(f"No environments defined ('ibmicli env set NAME'); '{NONE}' = none")
    default_env = (old.env if old else "") or cfg.active or NONE
    while True:
        answer = typer.prompt("Environment", default=default_env).strip()
        try:
            env = _env(cfg, answer)
            break
        except InvalidName as exc:
            output.error(str(exc))

    current = cfg.envs.get(env)
    curlib = current.curlib if current else ""
    typer.echo(f"Object library: '{NONE}' = environment current library "
               f"({curlib or 'not read yet'})")
    objlib = _library(typer.prompt(
        "Object library", default=(old.objlib if old and old.objlib else NONE)))
    return env, objlib


def _env(cfg: Config, name: str) -> str:
    name = name.strip()
    if not name or name == NONE:
        return ""
    if name not in cfg.envs:
        raise InvalidName(
            f"environment '{name}' does not exist "
            f"({', '.join(sorted(cfg.envs)) or 'none'}): create it with 'ibmicli env set {name}'"
        )
    return name


def _library(value: str) -> str:
    value = value.strip()
    if not value or value == NONE:
        return ""
    return check_object(value, "library")


def text(project: Project) -> str:
    """Project TOML contents with explanatory comments."""
    lines = [
        "# ibmicli project: this folder is the repository root.",
        "# Each app is a folder below it, containing LIBRARY/SOURCEFILE/MEMBER,",
        "# e.g. MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle. Commands read this with",
        "# -proj THIS_FILE (VS Code supplies it automatically).",
        "# Written by 'ibmicli set-project'; can also be edited manually.",
        "",
        "# Apps: env selects an environment ('ibmicli env ls'; empty means none,",
        "# for apps used only to read sources); objlib selects the object library",
        "# (empty uses the environment current library).",
        "# Per-app folder mappings belong in",
        "# [apps.NAME.libraries] and [apps.NAME.srcfile], overriding global mappings.",
        "# srclib makes a two-level app: no library folders, source files directly",
        "# below the app (APP/QRPGLESRC/MEMBER), all in that one library.",
    ]
    if not project.apps:
        lines += ["# [apps.MYAPP]", '# env = "dev"', '# objlib = ""']
    for app in sorted(project.apps.values(), key=lambda item: item.name.upper()):
        lines += [
            "",
            f"[apps.{_key(app.name)}]",
            f"env = {toml_str(app.env)}",
            f"objlib = {toml_str(app.objlib)}",
        ]
        if app.srclib:
            lines.append(f"srclib = {toml_str(app.srclib)}")
        for table, mapping in (("libraries", app.libraries), ("srcfile", app.srcfiles)):
            if mapping:
                lines.append(f"[apps.{_key(app.name)}.{table}]")
                lines += [f"{_key(project.spelling(app, folder))} = {toml_str(value)}"
                          for folder, value in sorted(mapping.items())]
    lines.append("")
    lines += _mapping_lines(
        "libraries", project.libraries,
        "Folder-to-library overrides for all apps.",
        'SOURCES = "MYAPP_SRC"', project.names,
    )
    lines.append("")
    lines += _mapping_lines(
        "srcfile", project.srcfiles,
        "Folder-to-source-file overrides for all apps.",
        'RPG = "QRPGLESRC"', project.names,
    )
    lines += [
        "",
        "# Repository tasks apply with -proj pointing to this file and override",
        "# global tasks with the same name. Step types and placeholders:",
        "# ibmicli tasks --guide",
    ]
    lines += task_lines(project.tasks)
    lines.append("")
    return "\n".join(lines)


def _mapping_lines(
    table: str, mapping: dict[str, str], comment: str, example: str, names: dict[str, str]
) -> list[str]:
    lines = [f"# {comment}"]
    if mapping:
        lines.append(f"[{table}]")
        lines += [f"{_key(names.get(folder, folder))} = {toml_str(value)}"
                  for folder, value in sorted(mapping.items())]
    else:
        lines += [f"# [{table}]", f"# {example}"]
    return lines


def _key(name: str) -> str:
    """Quote folder names when they are not bare TOML keys.

    A folder such as MY APP needs quotes or the whole project becomes unreadable.
    """
    return name if name.replace("_", "").replace("-", "").isalnum() and name.isascii() \
        else toml_str(name)


def _summary(cfg: Config, project: Project, app: App) -> None:
    env = prj.env_of(cfg, app)
    objlib = prj.object_library(app, env)
    missing = "" if app.root.is_dir() else "   (not created yet: pull creates it)"
    typer.echo(f"      folder             {app.root}{missing}")
    typer.echo(f"      environment        {app.env or '-   (source reads only; no compilation)'}")
    if objlib:
        typer.echo(f"      object library     {objlib}"
                   f"{'' if app.objlib else '   (current library)'}")
    elif env is not None:
        typer.echo(f"      object library     -   (missing: ibmicli env refresh {env.name})")
    known = cfg.known_srcfiles
    if app.srclib:
        files = sorted(entry.name for entry in (app.root.iterdir() if app.root.is_dir() else ())
                       if entry.is_dir() and project.srcfile_of(app, entry.name) in known)
        typer.echo(f"      source library     {app.srclib}   (no library folders)")
        typer.echo(f"      source files       {', '.join(files) or '-   (created by pull)'}")
        return
    libraries = []
    for entry in sorted(app.root.iterdir()) if app.root.is_dir() else ():
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        files = sorted(sub.name for sub in entry.iterdir()
                       if sub.is_dir() and project.srcfile_of(app, sub.name) in known)
        if files:
            libraries.append(f"{project.library_of(app, entry.name)} ({', '.join(files)})")
    typer.echo(f"      libraries          {'; '.join(libraries) or '-   (created by pull)'}")
