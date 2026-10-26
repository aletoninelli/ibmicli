"""Execute configured tasks through run NAME.

Flatten task inclusions before resolving requirements or executing steps.
CLI steps reenter Typer, sh steps use the local shell, job steps use RCMD
and normal CL uses QShell. Pin one FTP session so library-list setup persists.

Source and environment are required only when used by steps. Resolve values
from the explicit project or -var; never guess from an unrelated folder or
fall back to the active environment.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from .. import output, project as prj, session as sessions, tasks as tasks_mod
from ..cl import Runner
from ..config import Config, ConfigError, Env
from ..naming import InvalidName, check_object, split_file
from ..tasks import CL, JOB, QUOTED, SH, TASK, Task

# Detect recursive ibmicli run calls before they hang
# the CLI in an endless task cycle.
_running: set[str] = set()


def run(
    cfg: Config,
    name: str,
    source: str | None = None,
    dry_run: bool = False,
    verbose: bool = False,
    app: Any = None,
    given: dict[str, str] | None = None,
    project: prj.Project | None = None,
) -> int:
    """Run a named task, optionally on a local source.

    Project tasks override global ones. Explicit given values override app
    and folder values; env=NAME also selects setup and current library.
    Return 0 on success.
    """
    given = check_given(given or {})
    # Without a source, the current folder identifies the project app.
    # This lets standalone tasks in MYAPP/MYAPP_SRC/QRPGLESRC
    # use the MYAPP environment.
    start = Path(source) if source is not None else Path.cwd()
    available, _ = prj.tasks_for(cfg, project)

    task = cfg.require_task(name, available)
    if name in _running:
        raise ConfigError(f"task '{name}' calls itself: check its steps")

    trouble = tasks_mod.problems(task, known=set(available))
    if trouble:
        raise ConfigError(
            f"invalid task '{name}':\n       " + "\n       ".join(trouble)
        )

    # Flatten task inclusions before discovering requirements:
    # included steps can introduce additional placeholders.
    raw = _flatten(available, task)
    wanted = {holder for step in raw for holder in tasks_mod.used(step)}

    member, ext = "", ""
    found = prj.resolve(cfg, project, start)
    here_app = project.app_of(start) if project is not None else None
    if source is None:
        _refuse_without_source(task, wanted - set(given))
    else:
        member, ext = split_file(Path(source))
        if not task.accepts(ext):
            wanted_ext = " or ".join(f".{e}" for e in task.ext)
            raise InvalidName(
                f"'{source}' is not a {wanted_ext} source"
                + (f" (extension .{ext})" if ext else " (no extension)")
                + f"\n       task '{name}' accepts only {wanted_ext}"
            )

    env = (cfg.named_env(given["env"]) if "env" in given
           else _env(cfg, here_app))
    here = prj.library_at(project, start) if project is not None else ""
    values = _values(cfg, env, source, member, ext, found, here_app, here)
    values.update(given)
    # An explicit member override also controls the heading
    # and the EVFEVENT member read after compilation.
    member = values["member"]
    _refuse_missing(task, raw, values, env, project, here_app, start)
    steps = [(tasks_mod.kind_of(step), _expand(task, step, values)) for step in raw]

    output.info(_heading(name, env, member, values["objlib"]))

    _running.add(name)
    try:
        return _execute(
            cfg, task, env, member, steps, dry_run, verbose, app,
            Path(source) if source else None, values["objlib"], project,
        )
    finally:
        _running.discard(name)


# Validate and uppercase explicit IBM i object names
# just like project-derived names.
OBJECT_NAMES = {"member", "srclib", "srcfile", "objlib", "curlib"}


def check_given(given: dict[str, str]) -> dict[str, str]:
    """Validate explicit placeholder overrides."""
    checked: dict[str, str] = {}
    for name, value in given.items():
        if name not in tasks_mod.KNOWN:
            raise InvalidName(
                f"-var {name}: unknown placeholder ('ibmicli tasks --guide' lists names)"
            )
        checked[name] = check_object(value, f"{{{name}}}") if name in OBJECT_NAMES else value
    return checked


def _refuse_without_source(task: Task, wanted: set[str]) -> None:
    """Refuse tasks requiring a source when none is supplied."""
    needed = sorted(wanted & tasks_mod.SOURCE)
    if not needed:
        return
    raise InvalidName(
        f"task '{task.name}' requires a source: supply -src FILE\n"
        f"       required by placeholders "
        + ", ".join(f"{{{holder}}}" for holder in needed)
    )


def _env(cfg: Config, app: prj.App | None) -> Env | None:
    """App environment, or None.

    Using the active environment as a fallback could compile with another
    application's library list. _refuse_missing explains required values.
    """
    if app is not None and app.env:
        return cfg.named_env(app.env)
    # Outside a configured app environment, require explicit -var env=NAME.
    # No active-environment fallback is allowed.
    # _refuse_missing reports the missing value if needed.
    return None


def _refuse_missing(
    task: Task,
    raw: list[str],
    values: dict[str, Any],
    env: Env | None,
    project: prj.Project | None,
    app: prj.App | None,
    start: Path,
) -> None:
    """Report all missing values before execution.

    Values must come from -proj or -var. env is also required for CL when
    task.env is true, even if no placeholder names it.
    """
    missing: list[str] = []
    for step in raw:
        for holder in tasks_mod.used(step):
            if not values.get(holder) and holder not in missing:
                missing.append(holder)
    cl_steps = any(tasks_mod.kind_of(step) in (CL, JOB) for step in raw)
    if env is None and task.env and cl_steps and "env" not in missing:
        missing.append("env")
    if not missing:
        return

    folder = start.resolve() if start.is_dir() else start.resolve().parent
    # List explicit required values first.
    # -proj is a shortcut only for files in declared apps;
    # putting it first would mislead users working outside the repository.
    why = ""
    if project is None:
        pass
    elif app is None:
        why = f"{values.get('file') or folder} is outside the apps in {project.path}"
    elif {"srclib", "srcfile"} & set(missing) and values.get("file"):
        why = f"{values['file']} is outside LIBRARY/SOURCEFILE in app {app.name}"
    elif env is not None and {"objlib", "curlib"} & set(missing):
        why = (f"environment '{env.name}' has no current library: "
               f"ibmicli env refresh {env.name}, or set objlib in {app.where}")
    else:
        why = f"{app.where} does not supply these values"
    # env= also supplies current library and object-library defaults,
    # so do not request them twice.
    ask = [h for h in missing if not ("env" in missing and h in ("objlib", "curlib"))]
    note = ("  (objlib and curlib come from the environment)"
            if len(ask) < len(missing) else "")
    raise ConfigError(
        f"task '{task.name}' is missing "
        + ", ".join(f"{{{holder}}}" for holder in missing)
        + (f"\n       {why}" if why else "")
        + "\n       supply them with: -var "
        + " ".join(f"{holder}=VALUE" for holder in ask)
        + note
        + (prj.PROJ_HINT if project is None else "")
    )


def _heading(name: str, env: Env | None, member: str, objlib: str) -> str:
    """Heading containing available values only."""
    parts = [f"task {name}"]
    if env is not None:
        parts.append(f"env {env.name}")
    if member:
        parts.append(f"member {member}")
    line = "  ".join(parts)
    if objlib:
        line += f"  ->  {objlib}"
    return line


def _flatten(
    available: dict[str, Task], task: Task, chain: tuple[str, ...] = ()
) -> list[str]:
    """Flatten included tasks and report complete inclusion cycles."""
    if task.name in chain:
        chain_path = " -> ".join([*chain, task.name])
        raise ConfigError(f"task inclusion cycle: {chain_path}")

    steps: list[str] = []
    for step in task.steps:
        if tasks_mod.kind_of(step) != TASK:
            steps.append(step)
            continue
        wanted = tasks_mod.body(step)
        inner = available.get(wanted)
        if inner is None:
            raise ConfigError(
                f"task '{task.name}' includes missing 'task: {wanted}'\n"
                f"       available: {', '.join(available)}"
            )
        trouble = tasks_mod.problems(inner, known=set(available))
        if trouble:
            raise ConfigError(
                f"invalid task '{wanted}', included by '{task.name}':\n"
                "       " + "\n       ".join(trouble)
            )
        steps += _flatten(available, inner, (*chain, task.name))
    return steps


def _execute(
    cfg: Config,
    task: Task,
    env: Env | None,
    member: str,
    steps: list[tuple[str, str]],
    dry_run: bool,
    verbose: bool,
    app: Any,
    source: Path | None = None,
    objlib: str = "",
    project: prj.Project | None = None,
) -> int:
    # Pin one connection for the task unless the shell
    # already owns a shared connection.
    shared = not dry_run and not sessions.is_pinned()
    if shared:
        sessions.pin(cfg)
    try:
        # OPTION(*EVENTF) diagnostics use object library and member name.
        # A task compiling elsewhere can only show general messages
        # because that event member cannot be located.
        events = (objlib, member.upper()) if objlib and member else None
        with Runner(cfg, dry_run=dry_run, verbose=verbose, source=source, events=events,
                    project=project) as runner:
            # Set up immediately before the first CL, preserving step order.
            # A preceding deploy failure must not run environment setup
            # or touch IBM i unnecessarily.
            ready = False
            for kind, step in steps:
                if kind in (CL, JOB) and not ready:
                    if not _setup_environment(runner, task, env, member):
                        return 1
                    ready = True
                if not _step(runner, kind, step, dry_run, app):
                    output.fail(f"{member + ': ' if member else ''}'{task.name}' stopped")
                    return 1
    finally:
        if shared:
            sessions.unpin()

    output.ok(f"{member}  ->  {task.title}" if member else task.title)
    return 0


def _setup_environment(runner: Runner, task: Task, env: Env | None, member: str) -> bool:
    """Set up the environment before CL steps.

    Even fully qualified compilation commands need the library list for
    copies, DCLF and called programs; missing setup can cause SQL1002.
    """
    if not task.env or env is None:
        return True
    commands = env.commands
    if not commands:
        output.message(
            f"environment '{env.name}' has no setup: "
            "CL will use the default job library list"
        )
        return True
    # Use setup rather than run: only the FTP job preserves
    # the environment for subsequent commands.
    for command in commands:
        if not runner.setup(command):
            output.fail(f"{member + ': ' if member else ''}environment setup failed")
            return False
    return True


def _step(runner: Runner, kind: str, step: str, dry_run: bool, app: Any) -> bool:
    if kind == JOB:
        return runner.setup(step)
    if kind == CL:
        return runner.run(step).ok
    if kind == SH:
        return _shell(step, dry_run)
    return _cli(step, dry_run, app)


def _shell(step: str, dry_run: bool) -> bool:
    """Run sh steps in the task working folder with unfiltered output.

    Dry-run never starts local scripts: they can change files just as remote
    CL commands can change IBM i state.
    """
    output.command(f"sh: {step}")
    if dry_run:
        return True
    # Flush before starting a subprocess with its own stdout buffer,
    # so redirected output cannot place the command announcement
    # after the response it describes.
    sys.stdout.flush()
    shell = os.environ.get("SHELL") or "/bin/sh"
    try:
        return subprocess.run(step, shell=True, executable=shell, check=False).returncode == 0
    except KeyboardInterrupt:
        print("^C")
        return False
    except OSError as exc:
        output.fail(f"local shell unavailable: {exc}")
        return False


def _cli(step: str, dry_run: bool, app: Any) -> bool:
    """Reenter the same Typer application for ibmicli steps.

    Dry-run prints without invoking it: some commands have no -n option.
    """
    output.command(f"ibmicli {step}")
    if dry_run:
        return True
    if app is None:  # Invalid internal call; the CLI always supplies its app.
        output.fail("cannot run 'ibmicli' step: application unavailable")
        return False

    try:
        args = shlex.split(step)
    except ValueError as exc:
        output.fail(f"cannot parse step: {exc}")
        return False

    try:
        # standalone_mode preserves command-line help and errors;
        # SystemExit becomes the step status.
        app(args=args, prog_name="ibmicli", standalone_mode=True)
    except SystemExit as exc:
        code = exc.code
        return code in (0, None)
    return True


# --- Placeholders ----------------------------------------------------------

def _values(
    cfg: Config,
    env: Env | None,
    source: str | None,
    member: str,
    ext: str,
    found: prj.Target | None,
    app: prj.App | None = None,
    library: str = "",
) -> dict[str, str]:
    """Available placeholder values; empty values matter only when a step uses them."""
    path = Path(source) if source is not None else None
    if app is None and found is not None:
        app = found.app
    return {
        "member": member,
        "file": source or "",
        "dir": str(path.parent) if path is not None else "",
        "name": path.name if path is not None else "",
        "ext": ext.lower(),
        "srctype": cfg.srctype_for(ext) if ext else "",
        "srcfile": found.srcfile if found else "",
        "env": env.name if env is not None else "",
        "objlib": prj.object_library(app, env),
        # Library folder in the app; empty outside it.
        "srclib": found.library if found else library,
        "curlib": env.curlib if env is not None else "",
    }


def _expand(task: Task, step: str, values: dict[str, str]) -> str:
    """Replace only the placeholders used by this step, refusing missing values."""
    for name in tasks_mod.used(step):
        if values.get(name):
            continue
        raise InvalidName(f"task '{task.name}': {{{name}}} has no value")

    text = {key: value for key, value in values.items() if not key.startswith("_")}
    body = tasks_mod.body(step)
    if tasks_mod.kind_of(step) in QUOTED:
        # CLI and shell steps are split into arguments later.
        # Quote paths and other values containing spaces
        # before inserting them into the command text.
        return body.format(**{key: shlex.quote(value) for key, value in text.items()})
    return body.format(**text)
