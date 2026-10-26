"""Configure library-list environments.

Setup CL runs through RCMD in the FTP job; QShell liblist reads the resulting
current library and user portion. Explicit curlib/libl values generate setup
CL instead. Cached lists also support offline copy lookup and object-library
selection.
"""

from __future__ import annotations

import typer

from .. import config as cfgmod
from .. import output, session as sessions
from ..cl import Runner
from ..config import Config, ConfigError, Env
from ..naming import InvalidName, check_object
from ..session import SessionError

# Interactive setup mode choices.
BY_SETUP, BY_HAND = "cl", "list"


def show_active(cfg: Config) -> int:
    current = cfg.env()
    if current is None:
        if cfg.envs:
            output.error("no active environment; available: " + ", ".join(sorted(cfg.envs)))
        else:
            output.error("no environments defined; create one with: ibmicli env set NAME")
        return 1
    output.info(f"{current.name} (active)")
    show(current)
    return 0


def list_envs(cfg: Config) -> int:
    """Display all environment names and settings."""
    if not cfg.envs:
        output.error("no environments defined; create one with: ibmicli env set NAME")
        return 1
    names = sorted(cfg.envs)
    for index, name in enumerate(names):
        output.info(f"{name}{' (active)' if name == cfg.active else ''}")
        show(cfg.envs[name])
        if index < len(names) - 1:
            output.info("")
    return 0


def show(env: Env) -> None:
    if env.setup:
        for command in env.setup:
            typer.echo(f"  setup   {command}")
    elif env.commands:
        typer.echo(f"  setup   {env.commands[0]}   (from curlib and libl)")
    else:
        typer.echo("  setup   -   (no setup)")
    typer.echo(f"  CUR     {env.curlib or '-'}")
    if env.libl:
        for library in env.libl:
            typer.echo(f"  USR     {library}")
    else:
        typer.echo("  USR     -")
    if env.setup and not env.curlib and not env.libl:
        typer.echo(f"  library list not read yet: ibmicli env refresh {env.name}")


def set_env(cfg: Config, name: str | None = None, test: bool = True) -> int:
    """Prompt for an environment, then optionally test it."""
    if name:
        name = name.strip()
    else:
        if cfg.envs:
            typer.echo(f"Existing environments: {', '.join(sorted(cfg.envs))}")
        name = typer.prompt("Environment name").strip()
    if not name:
        output.error("environment name is required")
        return 2

    existing = cfg.envs.get(name)
    typer.echo(
        f"{'Edit' if existing else 'New'} environment '{name}'. How is the library list set?\n"
        f"  {BY_SETUP:<6} with your CL commands, e.g. CALL of an environment program:\n"
        f"         ibmicli reads the resulting library list from IBM i\n"
        f"  {BY_HAND:<6} by supplying current library and user library list"
    )
    default = BY_HAND if existing and not existing.setup else BY_SETUP
    while True:
        mode = typer.prompt(f"Mode ({BY_SETUP}/{BY_HAND})", default=default).strip().lower()
        if mode in (BY_SETUP, BY_HAND):
            break
        output.error(f"enter '{BY_SETUP}' or '{BY_HAND}'")
    try:
        env = _ask_setup(name, existing) if mode == BY_SETUP else _ask_hand(name, existing)
    except InvalidName as exc:
        output.error(str(exc))
        return 2

    if test:
        read = _read(cfg, env)
        if read is not None:
            env = read
        elif not typer.confirm("Save anyway?", default=bool(env.setup)):
            return 1

    cfg.envs[name] = env
    # The first environment becomes active automatically so db
    # and the shell can use it immediately.
    if cfg.active != name and (
        not cfg.active or typer.confirm(f"Use environment '{name}' now?", default=True)
    ):
        cfg.active = name
    cfgmod.save(cfg)
    output.ok(f"environment '{name}' saved to {cfg.path}")
    show(env)
    return 0


def _ask_setup(name: str, existing: Env | None) -> Env:
    """Collect setup commands until an empty line."""
    old = list(existing.setup) if existing else []
    typer.echo("Environment setup CL, one command per line; an empty line ends the list.")
    commands: list[str] = []
    while True:
        default = old[len(commands)] if len(commands) < len(old) else ""
        answer = typer.prompt(
            f"  CL {len(commands) + 1}", default=default, show_default=bool(default)
        ).strip()
        if not answer:
            break
        commands.append(answer)
    if not commands:
        raise InvalidName("no commands: choose 'list' mode to supply libraries manually")
    return Env(name=name, setup=tuple(commands),
               curlib=existing.curlib if existing else "",
               libl=existing.libl if existing else ())


def _ask_hand(name: str, existing: Env | None) -> Env:
    """Prompt for explicit current library and user library list."""
    old_cur = existing.curlib if existing else ""
    old_libl = " ".join(existing.libl) if existing else ""
    curlib = typer.prompt("Current library", default=old_cur, show_default=bool(old_cur)).strip()
    libl = typer.prompt(
        "Library list (user portion, in order, separated by spaces)",
        default=old_libl, show_default=bool(old_libl),
    ).strip()
    names = tuple(check_object(lib, "library") for lib in libl.replace(",", " ").split())
    return Env(name=name, curlib=check_object(curlib, "current library") if curlib else "",
               libl=names)


def _read(cfg: Config, env: Env) -> Env | None:
    """Run setup and read the resulting library list, or report failure and return None."""
    commands = env.commands
    if not commands:
        output.message(f"environment '{env.name}' has no setup to test")
        return env
    output.info(f"testing environment '{env.name}' on {cfg.host}")
    shared = not sessions.is_pinned()
    if shared:
        sessions.pin(cfg)
    try:
        with Runner(cfg) as runner:
            for command in commands:
                if not runner.setup(command):
                    output.fail(f"environment '{env.name}': setup failed")
                    return None
            try:
                found = runner.session.library_list()
            except SessionError as exc:
                output.fail(f"environment '{env.name}': could not read library list")
                output.note(str(exc))
                return None
    except SessionError as exc:
        output.fail(f"connection to {cfg.host} failed")
        output.note(str(exc))
        return None
    finally:
        if shared:
            sessions.unpin()

    curlib = next((lib for lib, kind in found if kind == "CUR"), "")
    libl = tuple(lib for lib, kind in found if kind == "USR")
    if not env.setup and (curlib != env.curlib or libl != env.libl):
        # CHGLIBL would reject invalid names. Differences such as QTEMP
        # come from IBM i additions, so keep the explicitly supplied
        # library list.
        output.message("IBM i reports this list (the supplied list remains configured):")
        for library, kind in found:
            if kind in ("CUR", "USR"):
                output.message(f"  {kind}  {library}")
        return env
    output.ok(f"environment '{env.name}': current library {curlib or '-'}, "
              f"{len(libl)} user libraries")
    if env.setup:
        return Env(name=env.name, setup=env.setup, curlib=curlib, libl=libl)
    return env


def use_env(cfg: Config, name: str | None) -> int:
    """Select the active environment, or list choices when no name is supplied."""
    if not cfg.envs:
        output.error("no environments defined; create one with: ibmicli env set NAME")
        return 2
    if not name:
        typer.echo("Available environments:")
        for env_name in sorted(cfg.envs):
            typer.echo(f"  {'*' if env_name == cfg.active else ' '} {env_name}")
        typer.echo("\nSelect one with: ibmicli env use NAME")
        return 0
    env = cfg.named_env(name.strip())
    cfg.active = env.name
    cfgmod.save(cfg)
    output.ok(f"active environment: '{env.name}'")
    show(env)
    return 0


def refresh(cfg: Config, name: str | None, everything: bool = False) -> int:
    """Read one or all environment library lists from IBM i."""
    if everything:
        targets = [cfg.envs[key] for key in sorted(cfg.envs)]
        if not targets:
            raise ConfigError("no environments defined; create one with: ibmicli env set NAME")
    elif name:
        targets = [cfg.named_env(name.strip())]
    else:
        targets = [cfg.require_env()]

    failed = 0
    for env in targets:
        read = _read(cfg, env)
        if read is None:
            failed += 1
            continue
        cfg.envs[env.name] = read
        # Save each result so an interrupted refresh retains completed environments.
        cfgmod.save(cfg)
        show(read)
    return 1 if failed else 0
