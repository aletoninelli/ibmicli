"""Prompt for and save global connection settings."""

from __future__ import annotations

import typer

from .. import config as cfgmod
from ..config import Config
from ..session import Session, SessionError
from . import db as db_cmd
from . import debug as debug_cmd


def run(force: bool = False, test: bool = True) -> int:
    path = cfgmod.config_path()
    existing: Config | None = None
    if path.is_file():
        try:
            existing = cfgmod.load()
        except cfgmod.ConfigError:
            existing = None
        if not force:
            typer.echo(f"Configuration already exists at {path}")
            if not typer.confirm("Replace connection settings?", default=False):
                return 0

    typer.echo("IBM i connection settings (saved only in this local configuration file).")
    host = typer.prompt("Hostname or IP", default=existing.host if existing else None)
    user = typer.prompt("User", default=existing.user if existing else None)
    password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    ccsid = typer.prompt(
        "Override CCSID (empty = job default)",
        default=(existing.ccsid if existing and existing.ccsid else ""),
        show_default=bool(existing and existing.ccsid),
    ).strip()
    debug_host, debug_batch = _debug(existing, test)

    cfg = Config(
        host=host.strip(),
        user=user.strip(),
        password=password,
        path=path,
        ccsid=ccsid or None,
        set_srctype=existing.set_srctype if existing else True,
        local_charset=existing.local_charset if existing else "utf-8",
        remote_charset=existing.remote_charset if existing else "iso-8859-1",
        debug_host=debug_host,
        debug_batch=debug_batch,
        srctype=dict(existing.srctype) if existing else {},
        pull_ext=dict(existing.pull_ext) if existing else {},
        # Preserve environments when changing connection settings.
        envs=dict(existing.envs) if existing else {},
        # Preserve user tasks; never create implicit default tasks.
        tasks=dict(existing.tasks) if existing else {},
        build=dict(existing.build) if existing else {},
        tasks_key=existing.tasks_key if existing else cfgmod.TASKS_KEY,
        active=existing.active if existing else "",
    )

    if test:
        typer.echo(f"Testing connection to {cfg.host}...")
        try:
            with Session(cfg) as session:
                session.ftp  # Open and authenticate.
            typer.echo("Connection succeeded.")
        except SessionError as exc:
            typer.echo(f"Connection failed: {exc}", err=True)
            if not typer.confirm("Save anyway?", default=True):
                return 1
        _odbc(cfg)

    saved = cfgmod.save(cfg)
    typer.echo(f"Configuration saved to {saved} (mode 600).")
    typer.echo("Commands now work from any folder:")
    typer.echo("  ibmicli deploy -lib MYAPP_SRC -file QRPGLESRC -src HELLO.sqlrpgle")
    typer.echo("Next: 'ibmicli env set NAME' for the environment library list, then")
    typer.echo("'ibmicli set-project MYAPP -proj ibmiproj.toml' to map an app.")
    if not cfg.tasks:
        typer.echo("")
        typer.echo("No tasks are configured; define the compilation steps you need.")
        typer.echo("See 'ibmicli tasks --guide' for step types and placeholders.")
    return 0


def _odbc(cfg: Config) -> None:
    """Check ODBC during initial setup without blocking FTP usage.

    The IBM driver is installed separately; report missing components before
    the first query. All other CLI commands work without it.
    """
    typer.echo("Testing the database (ODBC, required by 'ibmicli db')...")
    if db_cmd.check(cfg) != 0:
        typer.echo("Without ODBC, all commands except 'ibmicli db' remain available.")
        typer.echo("After fixing the problem, run: ibmicli db --check")


def _debug(existing: Config | None, test: bool) -> tuple[str, str]:
    """Prompt for optional debug hostname and batch command during setup.

    Validate TLS immediately: an IP can appear to work for batch debugging
    but fail when the IBM extension verifies a SEP connection. An empty
    hostname skips debug configuration.
    """
    old_host = existing.debug_host if existing else ""
    old_batch = existing.debug_batch if existing else ""

    typer.echo("")
    typer.echo("VS Code debug (optional: press Enter with an empty value to skip).")
    typer.echo("Use the system HOSTNAME covered by the Debug Service certificate;")
    typer.echo("an IP address prevents service-entry-point attachment.")
    while True:
        host = typer.prompt(
            "Debug Service hostname", default=old_host, show_default=bool(old_host)
        ).strip()
        if not host:
            return "", old_batch
        if debug_cmd.is_ip(host):
            typer.echo(f"{host} is an IP address: supply a hostname, e.g. ibmi.example.com", err=True)
            continue
        if not test or _host_ok(host):
            break

    proposed = old_batch or debug_cmd.BATCH_EXAMPLE
    typer.echo("Command to submit the program for batch debugging,")
    typer.echo("using task placeholders ('ibmicli tasks --guide'). Enter accepts:")
    typer.echo(f"  {proposed}")
    while True:
        batch = typer.prompt("Batch command", default=proposed, show_default=False).strip()
        trouble = debug_cmd.batch_problems(batch)
        if not trouble:
            return host, batch
        for problem in trouble:
            typer.echo(f"Invalid: {problem}", err=True)


def _host_ok(host: str) -> bool:
    """Test the hostname and return whether to retain it."""
    typer.echo(f"Testing Debug Service at {host}:{debug_cmd.DEBUG_PORT}...")
    outcome, detail = debug_cmd.check_host(host)
    if outcome == debug_cmd.HOST_OK:
        typer.echo("Certificate verified.")
        return True
    if outcome == debug_cmd.HOST_CERTIFICATE:
        # The system replied, but its certificate does not cover this hostname.
        # Keeping it would prevent service-entry-point attachment.
        typer.echo(f"Certificate does not cover {host}: {detail}", err=True)
        return typer.confirm("Keep it anyway?", default=False)
    # An unreachable host can mean the local network is currently unavailable.
    typer.echo(f"Debug Service unreachable: {detail}", err=True)
    return typer.confirm("Keep it anyway?", default=True)
