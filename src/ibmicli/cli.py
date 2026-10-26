"""CLI entry point."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from typer.core import TyperGroup

from . import __version__, config as cfgmod, output, project as prj, shell
from .commands import buildchanged as buildchanged_cmd
from .commands import config as config_cmd
from .commands import copy as copy_cmd
from .commands import db as db_cmd
from .commands import debug as debug_cmd
from .commands import diff as diff_cmd
from .commands import show as show_cmd
from .commands import deploy as deploy_cmd
from .commands import env as env_cmd
from .commands import list as list_cmd
from .commands import pull as pull_cmd
from .commands import run as run_cmd
from .commands import setconn as setconn_cmd
from .commands import setproject as setproject_cmd
from .commands import symbols as symbols_cmd
from .commands import tasks as tasks_cmd
from .commands import vscode as vscode_cmd
from .config import Config, ConfigError
from .copybook import CopyError
from .naming import InvalidName, check_object, parse_target

class Commands(TyperGroup):
    """Translate expected errors for every command in one place.

    ConfigError, InvalidName and CopyError produce ERROR and exit code 2.
    Commands raise them directly so new commands cannot accidentally expose
    a traceback by forgetting their own error handler.
    """

    def invoke(self, ctx: typer.Context) -> object:
        try:
            return super().invoke(ctx)
        except (ConfigError, InvalidName, CopyError) as exc:
            output.error(str(exc))
            raise typer.Exit(2) from None


app = typer.Typer(
    cls=Commands,
    # No arguments enters the interactive shell; see _main.
    no_args_is_help=False,
    add_completion=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    help=(
        "Work with IBM i source members.\n\n"
        "Install once and run from any folder. "
        "Supply library and source file as options or through -proj FILE. "
        "Connection settings live in the user configuration.\n\n"
        "Run 'ibmicli' without arguments to open the interactive shell "
        "and reuse one connection across commands."
    ),
)

# Deploy can resolve library and source file from an explicit project
# and the local file path; see project.py.
LibOpt = Annotated[
    Optional[str],
    typer.Option("-lib", "--lib", "--library", metavar="LIBRARY", show_default=False,
                 help="IBM i source library, e.g. MYAPP_SRC; otherwise resolved from the project."),
]
FileOpt = Annotated[
    Optional[str],
    typer.Option("-file", "--file", "--srcfile", metavar="SRCFILE", show_default=False,
                 help="Source file, e.g. QRPGLESRC; otherwise resolved from the project path."),
]
# Pull can replace library, source file and member
# with the complete remote -path.
PullLibOpt = Annotated[
    Optional[str],
    typer.Option("-lib", "--lib", "--library", metavar="LIBRARY", show_default=False,
                 help="Source library; not needed with -path."),
]
PullFileOpt = Annotated[
    Optional[str],
    typer.Option("-file", "--file", "--srcfile", metavar="SRCFILE", show_default=False,
                 help="Source file; not needed with -path."),
]
# List can resolve library and source file from the current folder
# when an explicit project is supplied.
ListLibOpt = Annotated[
    Optional[str],
    typer.Option("-lib", "--lib", "--library", metavar="LIBRARY", show_default=False,
                 help="Source library; otherwise resolved from the project."),
]
ListFileOpt = Annotated[
    Optional[str],
    typer.Option("-f", "-file", "--file", "--srcfile", metavar="SRCFILE", show_default=False,
                 help=f"Source file; otherwise the current project folder. "
                      f"{list_cmd.ALL} = all source files in the library."),
]
# The project is an explicitly selected repository-root ibmiproj.toml.
# The CLI never searches for it.
# The VS Code extension supplies it.
ProjOpt = Annotated[
    Optional[Path],
    typer.Option("-proj", "--proj", metavar="FILE", show_default=False,
                 help="Repository ibmiproj.toml file: apps, folder mappings and tasks."),
]
DryRunOpt = Annotated[
    bool, typer.Option("-n", "--dry-run", help="Show the transfer mapping without transferring.")
]
AllFilesOpt = Annotated[
    bool, typer.Option("-all", "--all", help="All files in the current folder.")
]
AllMembersOpt = Annotated[
    bool, typer.Option("-all", "--all", help="All members of the source file.")
]


# Deploy and run operate on local files with -src.
# Hidden member aliases remain for existing task commands.
# Pull uses -mbr for remote members.
# Help shows only -src for local sources.
OldMbrOpt = Annotated[
    Optional[str], typer.Option("-mbr", "--mbr", "--member", hidden=True, show_default=False),
]


def _source(src: str | None, old: str | None) -> str | None:
    if src and old:
        raise InvalidName("-src and -mbr select the same value: use only -src")
    return src or old


def _config() -> Config:
    """Load configuration; Commands.invoke handles missing or invalid values."""
    return cfgmod.load()


def _tasks_key() -> str:
    """Task shortcut, allowing installation before configuration exists.

    vscode install must also work on a new machine before set-conn.
    """
    try:
        return cfgmod.load().tasks_key
    except ConfigError:
        return cfgmod.TASKS_KEY


def _names(library: str, srcfile: str) -> tuple[str, str]:
    return check_object(library, "library"), check_object(srcfile, "source file")


def _deploy_names(
    cfg: Config,
    project: prj.Project | None,
    library: str | None,
    srcfile: str | None,
    targets: list[Path],
) -> tuple[str, str]:
    """Deploy library and source file from explicit options or the project.

    Explicit values win. All files must share one folder unless both values
    are supplied: choosing a destination for mixed folders would be ambiguous.
    """
    if library and srcfile:
        return _names(library, srcfile)

    folders = {path.resolve().parent for path in targets} or {Path.cwd()}
    if len(folders) > 1:
        raise ConfigError(
            "files are in different folders: "
            "supply -lib and -file, or deploy one folder at a time"
        )
    folder = folders.pop()
    found = prj.resolve(cfg, project, folder)
    if found is None:
        raise _no_project(project, folder, "library and source file: -lib LIBRARY -file SRCFILE")
    output.info(_where(found))
    return _names(library or prj.source_library(found), srcfile or found.srcfile)


def _where(found: prj.Target) -> str:
    """Describe where library and source file were resolved."""
    return f"app {found.app.name}"


def _no_project(project: prj.Project | None, folder: Path, what: str) -> ConfigError:
    """Request explicit values when the project cannot resolve the folder."""
    if project is None:
        return prj.no_project(f"missing {what}")
    return ConfigError(
        f"{folder} is outside APP/LIBRARY/SOURCEFILE in {project.path}\n"
        f"       supply {what}"
    )


def _pull_names(
    cfg: Config,
    project: prj.Project | None,
    library: str | None,
    srcfile: str | None,
    folder: Path,
) -> tuple[str, str]:
    """Pull library and source file from options or the destination folder.

    The destination is -o or the current folder. If remote names are supplied
    instead, _pull_dest determines the local destination from them.
    """
    if library and srcfile:
        return _names(library, srcfile)

    found = prj.resolve(cfg, project, folder)
    if found is None:
        raise _no_project(project, folder.resolve(),
                          "library and source file: -lib LIBRARY -file SRCFILE (or -path)")
    output.info(_where(found))
    return _names(library or prj.source_library(found), srcfile or found.srcfile)


def _pull_dest(project: prj.Project | None, library: str, srcfile: str) -> Path:
    """Default download folder, determined only by the current location.

    Inside a declared app, use APP/LIBRARY/SOURCEFILE with project mappings.
    Elsewhere use LIBRARY/SOURCEFILE below the current folder, reusing an
    existing library or source-file folder instead of nesting it again.
    """
    here = Path.cwd()
    app = project.app_of(here) if project is not None else None
    folder = prj.place(project, app, library, srcfile) if app is not None else None
    if folder is None:
        folder = _local_dest(here, library, srcfile)
    if folder != here:
        output.info(f"folder {folder}")
    return folder


def _local_dest(here: Path, library: str, srcfile: str) -> Path:
    """LIBRARY/SOURCEFILE below here, reusing matching parent folders."""
    lib, srcf = library.upper(), srcfile.upper()
    if here.name.upper() == srcf and here.parent.name.upper() == lib:
        return here
    if here.name.upper() == lib:
        return prj._child(here, srcf)
    return prj._child(prj._child(here, lib), srcf)


def _selection(
    mbr: str | None, mbrs: str | None, everything: bool, names: tuple[str, str] = ("-mbr", "-mbrs")
) -> list[str]:
    """Require exactly one transfer selector.

    Pull uses -mbr/-mbrs; deploy uses -src/-srcs. Requiring an explicit
    selector prevents a missing argument from transferring an entire folder.
    """
    one, many = names
    what = "FILE" if one == "-src" else "NAME"
    used = [name for name, value in ((one, mbr), (many, mbrs), ("-all", everything)) if value]
    if not used:
        output.error(
            f"select what to transfer: {one} {what} (one), "
            f"{many} {what}1,{what}2 (list) or -all"
        )
        raise typer.Exit(2)
    if len(used) > 1:
        output.error(f"{' and '.join(used)} cannot be combined: choose one")
        raise typer.Exit(2)

    if everything:
        return []  # Empty means all.
    raw = mbr if mbr else (mbrs or "")
    names = [piece.strip() for piece in raw.split(",") if piece.strip()]
    if not names:
        output.error("empty member list")
        raise typer.Exit(2)
    return names


def _version(value: bool) -> None:
    if value:
        print(f"ibmicli {__version__}")
        print(f"configuration: {cfgmod.config_path()}")
        # Version information remains available with missing or broken
        # configuration; report the problem without aborting.
        try:
            print(f"host: {cfgmod.load().host}")
        except ConfigError as exc:
            print(f"host: unavailable ({str(exc).splitlines()[0]})")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    version: Annotated[
        Optional[bool],
        typer.Option("--version", callback=_version, is_eager=True, help="Show the version."),
    ] = None,
) -> None:
    # No subcommand starts the shell.
    if ctx.invoked_subcommand is None:
        raise typer.Exit(shell.run(app))


@app.command()
def deploy(
    library: LibOpt = None,
    srcfile: FileOpt = None,
    source: Annotated[
        Optional[str],
        typer.Option("-src", "--src", metavar="FILE", show_default=False,
                     help="One local file to upload, e.g. QRPGLESRC/HELLO.sqlrpgle."),
    ] = None,
    sources: Annotated[
        Optional[str],
        typer.Option("-srcs", "--srcs", metavar="LIST", show_default=False,
                     help="Comma-separated local files."),
    ] = None,
    old_member: OldMbrOpt = None,
    old_members: Annotated[
        Optional[str], typer.Option("-mbrs", "--mbrs", hidden=True, show_default=False),
    ] = None,
    everything: AllFilesOpt = False,
    dry_run: DryRunOpt = False,
    srctype: Annotated[
        Optional[str],
        typer.Option("-t", "--srctype", metavar="TYPE", show_default=False,
                     help="Override the SRCTYPE derived from the extension."),
    ] = None,
    proj: ProjOpt = None,
) -> None:
    """Upload local files to IBM i source members.

    Example: ibmicli deploy -lib MYAPP_SRC -file QRPGLESRC -src HELLO.sqlrpgle -n

    Choose one of -src, -srcs or -all. The member name is the uppercase file
    stem; SRCTYPE comes from its extension unless overridden with -t.
    With -proj FILE, a file in a declared app supplies -lib and -file from
    its LIBRARY/SOURCEFILE folders. Use -n to inspect the transfer first.
    """
    cfg = _config()
    project = prj.load(proj)
    selected = _selection(_source(source, old_member), _source(sources, old_members),
                          everything, ("-src", "-srcs"))
    if everything:
        targets = _folder_files()
        if not targets:
            output.error(f"no files to upload in {Path.cwd()}")
            raise typer.Exit(2)
    else:
        targets = [Path(name) for name in selected]
    lib, srcf = _deploy_names(cfg, project, library, srcfile, targets)
    failures = deploy_cmd.run(cfg, lib, srcf, targets, dry_run=dry_run, srctype=srctype)
    raise typer.Exit(1 if failures else 0)


def _folder_files() -> list[Path]:
    """Visible files in the current folder, returned as relative paths."""
    return sorted(
        Path(entry.name)
        for entry in Path.cwd().iterdir()
        if entry.is_file() and not entry.name.startswith(".")
    )


@app.command()
def pull(
    library: PullLibOpt = None,
    srcfile: PullFileOpt = None,
    member: Annotated[
        Optional[str],
        typer.Option("-mbr", "--mbr", "--member", metavar="MEMBER", show_default=False,
                     help="One member, e.g. HELLO or HELLO.sqlrpgle."),
    ] = None,
    members: Annotated[
        Optional[str],
        typer.Option("-mbrs", "--mbrs", metavar="LIST", show_default=False,
                     help="Comma-separated member names."),
    ] = None,
    everything: AllMembersOpt = False,
    remote_path: Annotated[
        Optional[str],
        typer.Option("-path", "--path", metavar="LIBRARY/FILE,MEMBER", show_default=False,
                     help="Complete member path in the format printed by 'list'."),
    ] = None,
    out: Annotated[
        Optional[Path],
        typer.Option("-o", "--out", metavar="FOLDER", show_default=False,
                     help="Destination folder; otherwise LIBRARY/SOURCEFILE under the current location."),
    ] = None,
    dry_run: DryRunOpt = False,
    force: Annotated[
        bool, typer.Option("-f", "--force", help="Overwrite existing local files.")
    ] = False,
    proj: ProjOpt = None,
) -> None:
    """Download source members to local files.

    Example: ibmicli pull -path MYAPP_SRC/QRPGLESRC,HELLO -n

    Use -path, or supply -lib/-file and one of -mbr, -mbrs or -all.
    Extensions come from the source-file mapping unless given in the name.
    Without -o, use LIBRARY/SOURCEFILE below the current location; inside
    a declared project app, use that app and its folder mappings.
    With -proj, -lib/-file can be omitted in a source-file folder.
    Existing local files require -f to overwrite.
    """
    cfg = _config()
    project = prj.load(proj)
    if remote_path:
        old_options = [
            name
            for name, value in (
                ("-lib", library),
                ("-file", srcfile),
                ("-mbr", member),
                ("-mbrs", members),
                ("-all", everything),
            )
            if value
        ]
        if old_options:
            output.error(f"-path cannot be combined with {', '.join(old_options)}")
            raise typer.Exit(2)
        lib, srcf, selected_member = parse_target(remote_path)
        selected = [selected_member]
        dest = out if out is not None else _pull_dest(project, lib, srcf)
    else:
        here = out if out is not None else Path.cwd()
        lib, srcf = _pull_names(cfg, project, library, srcfile, here)
        selected = _selection(member, members, everything)
        # With explicit remote names, compute the default destination.
        # Otherwise _pull_names resolved the destination itself:
        # here is -o or the current folder.
        if out is None and library and srcfile:
            dest = _pull_dest(project, lib, srcf)
        else:
            dest = here
    failures = pull_cmd.run(cfg, lib, srcf, selected, dest, dry_run=dry_run, force=force)
    raise typer.Exit(1 if failures else 0)


@app.command("list")
def list_members(
    library: ListLibOpt = None,
    srcfile: ListFileOpt = None,
    proj: ProjOpt = None,
) -> None:
    """List members, one LIBRARY/FILE,MEMBER per line.

    Example: ibmicli list -lib MYAPP_SRC -f QRPGLESRC

    With -proj, a source-file folder supplies library and source file.
    Use -f '*ALL' to list all source files in the library. Lines can be
    passed directly to show or pull -path.
    """
    raise typer.Exit(list_cmd.run(_config(), library, srcfile, prj.load(proj)))


@app.command("show")
def show_member(
    targets: Annotated[
        list[str],
        typer.Argument(metavar="LIBRARY/FILE,MEMBER", show_default=False,
                       help="Member in the format printed by 'list'."),
    ],
    edit: Annotated[
        Optional[str],
        typer.Option("-e", "-edit", "--edit", metavar="EDITOR", show_default=False,
                     help="Open a temporary read-only copy: nano or vsc."),
    ] = None,
) -> None:
    """Write a source member to stdout.

    Example: ibmicli show MYAPP_SRC/QRPGLESRC,HELLO

    Accepts LIBRARY/FILE,MEMBER and LIBRARY/FILE(MEMBER).
    Use shell redirection or a pipe to process the text. With -edit nano
    or -edit vsc, open a temporary read-only copy instead.
    """
    editor = edit.lower() if edit else None
    if editor and editor not in show_cmd.EDITORS:
        output.error(f"invalid editor '{edit}': use nano or vsc")
        raise typer.Exit(2)
    raise typer.Exit(1 if show_cmd.run(_config(), targets, editor) else 0)


@app.command("diff")
def diff_member(
    files: Annotated[
        list[Path],
        typer.Argument(metavar="FILE...", show_default=False,
                       help="Local files to compare with remote members."),
    ],
    library: LibOpt = None,
    srcfile: FileOpt = None,
    context: Annotated[
        int, typer.Option("-U", "--context", metavar="N", min=0,
                          help="Context lines around each difference."),
    ] = 3,
    as_json: Annotated[
        bool, typer.Option("--json", help="Member content and result, one JSON line per file (VS Code)."),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("-n", "--dry-run", help="Show the remote member without connecting."),
    ] = False,
    proj: ProjOpt = None,
) -> None:
    """Compare local files with their IBM i members in memory.

    Example: ibmicli diff HELLO.sqlrpgle -lib MYAPP_SRC -file QRPGLESRC -n

    The direction matches deploy: '-' is on IBM i; '+' is local.
    With -proj, the local path supplies library and source file.
    Exit 0 means equal; 1 means different or unreadable. For a whole
    project folder use status.
    """
    raise typer.Exit(
        diff_cmd.run_diff(
            _config(), files, library, srcfile, as_json=as_json, dry_run=dry_run, context=context,
            project=prj.load(proj),
        )
    )


@app.command("status")
def status(
    folder: Annotated[
        Optional[Path],
        typer.Argument(metavar="[FOLDER]", show_default=False,
                       help="Starting folder; defaults to the current folder."),
    ] = None,
    remote_too: Annotated[
        bool, typer.Option("-remote", "--remote",
                           help="Also list remote-only members."),
    ] = False,
    verbose: Annotated[
        bool, typer.Option("-v", "--verbose", help="Also show equal files."),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("-n", "--dry-run", help="Show folders without connecting."),
    ] = False,
    proj: ProjOpt = None,
) -> None:
    """Show project files that differ from IBM i members.

    Example: ibmicli status -proj ibmiproj.toml -n

    Requires -proj. A source-file folder compares that folder; an app or
    project root includes known source-file folders below it.
    DIFF shows changed lines, LOCAL means absent on IBM i. Remote-only
    members are counted; -remote also lists them as IBMI. Use -v to show
    equal files. Exit 0 means all equal; 1 means differences or failures.
    """
    raise typer.Exit(
        diff_cmd.run_status(
            _config(), folder or Path.cwd(), remote_too=remote_too, verbose=verbose,
            dry_run=dry_run, project=prj.load(proj),
        )
    )


@app.command("copy")
def copy_member(
    specs: Annotated[
        Optional[list[str]],
        typer.Argument(metavar="[LIBRARY/FILE,MEMBER]...", show_default=False,
                       help="The /copy directives to resolve; complete lines are accepted."),
    ] = None,
    origin: Annotated[
        Optional[Path],
        typer.Option("-from", "--from", metavar="SOURCE", show_default=False,
                     help="Source containing the directives; defaults to the current folder."),
    ] = None,
    everything: Annotated[
        bool, typer.Option("-all", "--all", help="All directives in the -from source.")
    ] = False,
    pull: Annotated[
        Optional[bool],
        typer.Option("-pull/-no-pull", "--pull/--no-pull", show_default=False,
                     help="Download missing members; disabled by default with -all."),
    ] = None,
    open_editor: Annotated[
        bool, typer.Option("-open", "--open", help="Open resolved copies in VS Code.")
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Output for the VS Code extension, one JSON line.")
    ] = False,
    dry_run: DryRunOpt = False,
    proj: ProjOpt = None,
) -> None:
    """Resolve /copy directives to local files, downloading missing members.

    Example: ibmicli copy -from MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -all -no-pull -proj ibmiproj.toml

    Requires -proj. Library folders are searched across declared apps.
    Libraries absent from all apps are placed in the source app.
    Unqualified directives follow the source app's environment library list.
    A single copy downloads by default; -all downloads only with -pull.
    """
    cfg = _config()
    if everything and specs:
        output.error("-all and an explicit copy list cannot be combined: choose one")
        raise typer.Exit(2)
    if not everything and not specs:
        output.error("supply a copy (LIBRARY/FILE,MEMBER) or -all with -from SOURCE")
        raise typer.Exit(2)

    source = origin if origin is not None else Path.cwd()
    if everything and not source.is_file():
        output.error("-all requires a source to read: -from FILE")
        raise typer.Exit(2)

    raise typer.Exit(
        copy_cmd.run(
            cfg,
            specs or [],
            source,
            everything=everything,
            # Scanning a whole source does not imply authorization to download.
            # An explicit -pull enables transfers for -all.
            pull=(not everything) if pull is None else pull,
            open_editor=open_editor,
            as_json=as_json,
            dry_run=dry_run,
            project=prj.load(proj),
        )
    )


@app.command("symbols")
def show_symbols(
    source: Annotated[
        Path,
        typer.Argument(metavar="SOURCE", show_default=False,
                       help="Source to read, e.g. MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle."),
    ],
    only_copies: Annotated[
        bool,
        typer.Option("-copies", "--copies",
                     help="Only symbols from copies, excluding the source itself."),
    ] = False,
    kind: Annotated[
        Optional[str],
        typer.Option("-k", "--kind", metavar="TYPE", show_default=False,
                     help="One kind: prototype, procedure, variable, constant, struct, "
                          "subfield, file, subroutine."),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Output for the VS Code extension, one JSON line.")
    ] = False,
    local: Annotated[
        bool,
        typer.Option("-local", "--local",
                     help="Only declarations in the source; do not follow copies."),
    ] = False,
    stdin_text: Annotated[
        bool, typer.Option("--stdin", help="Read source text from stdin (the editor)."),
    ] = False,
    proj: ProjOpt = None,
) -> None:
    """List free-format RPG declarations, following /copy directives recursively.

    Example: ibmicli symbols MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -proj ibmiproj.toml

    Supports completion and Outline in VS Code. Unreadable copies are
    reported with their reasons. Download missing members with copy -all
    -pull. Without -proj, copies cannot be resolved. Use -local for this
    source only. Fixed-format specifications are not parsed.
    """
    raise typer.Exit(
        symbols_cmd.run(
            _config(), source, only_copies=only_copies, as_json=as_json, kind=kind,
            local=local, stdin_text=stdin_text, project=prj.load(proj),
        )
    )


@app.command("debug")
def debug_program(
    source: Annotated[
        Path,
        typer.Argument(metavar="SOURCE", show_default=False,
                       help="Program source, e.g. MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle."),
    ],
    sep: Annotated[
        bool,
        typer.Option("-sep", "--sep",
                     help="For a service entry point: no batch command needed."),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Output for the VS Code extension, one JSON line.")
    ] = False,
    proj: ProjOpt = None,
) -> None:
    """Prepare batch or service-entry-point debugging for VS Code.

    Example: ibmicli debug MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -sep -proj ibmiproj.toml

    This command is offline: it reports the program, library and system.
    --json supplies the launch configuration to the IBM i Debug extension;
    credentials remain in memory. -proj provides the app environment.

    Set debug_host to the system hostname covered by its TLS certificate,
    not its IP address. Set debug_batch to a submission command using task
    placeholders (tasks --guide); it is not needed with -sep.
    Both settings belong before any TOML table in global configuration.
    Compile the program with DBGVIEW(*SOURCE) or *ALL.
    """
    raise typer.Exit(debug_cmd.run(_config(), source, sep=sep, as_json=as_json,
                                   project=prj.load(proj)))


@app.command("db")
def query_db(
    sql: Annotated[
        Optional[list[str]],
        typer.Argument(metavar="[SQL]", show_default=False,
                       help="SQL statement, preferably quoted. Omit for sql> prompt or stdin."),
    ] = None,
    libraries: Annotated[
        Optional[str],
        typer.Option("-lib", "--lib", "--library", metavar="LIB[,LIB...]", show_default=False,
                     help="Libraries to add to the list; otherwise use the environment."),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Only JSON on stdout, one line per statement.")
    ] = False,
    as_csv: Annotated[
        bool, typer.Option("--csv", help="CSV with a header.")
    ] = False,
    out: Annotated[
        Optional[str],
        typer.Option("-o", "--out", metavar="FILE", show_default=False,
                     help="Write results to this file, overwritten after each successful query. "
                          "Without --csv/--json, the extension selects .csv, .json, "
                          "or a table."),
    ] = None,
    write: Annotated[
        bool, typer.Option("--write", help="Allow writes: INSERT, UPDATE, DELETE... (committed immediately)."),
    ] = False,
    tx: Annotated[
        bool, typer.Option("-t", "--tx", help="With --write: transaction; 'commit' in the prompt confirms, exit rolls back."),
    ] = False,
    check: Annotated[
        bool, typer.Option("--check", help="Check the ODBC driver and connection, with installation guidence."),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("-n", "--dry-run", help="Show connection settings and SQL without connecting.")
    ] = False,
    source: Annotated[
        Optional[Path],
        typer.Option("-from", "--from", metavar="SOURCE", show_default=False,
                     help="Read SQL from a source (.sql or RPG exec sql); "
                          "use its project app's environment libraries."),
    ] = None,
    line: Annotated[
        Optional[int],
        typer.Option("-line", "--line", metavar="N", show_default=False, min=1,
                     help="With -from: the statement at line N."),
    ] = None,
    stdin_text: Annotated[
        bool, typer.Option("--stdin", help="With -from and -line: read source text from stdin."),
    ] = False,
    variables: Annotated[
        Optional[list[str]],
        typer.Option("--var", metavar="NAME=VALUE", show_default=False,
                     help="With -from: a host variable value, written as SQL "
                          "(--var CODE=\"'A01'\"). Repeatable."),
    ] = None,
    proj: ProjOpt = None,
) -> None:
    """Run SQL through the IBM i Access ODBC driver.

    Example: ibmicli db "select 1 from sysibm.sysdummy1" -n

    No SQL starts the sql> prompt, or reads stdin when redirected.
    --check diagnoses driver installation and connectivity.
    The connection is read-only by default. --write enables changes with
    immediate commit. Add --tx for a transaction: commit or rollback in the
    prompt; exiting rolls back uncommitted work. Tables need journaling.

    -from extracts SQL from a source; -line selects its statement.
    RPG SELECT INTO is reduced to SELECT, cursors to their query, and host
    variables use --var SQL values. --stdin supplies editor buffer text.
    With -proj, library names come from the source or current folder's app;
    otherwise db uses the active environment. -lib supplies libraries.
    --out writes each successful result to the requested file.
    """
    if check:
        try:
            cfg: Config | None = cfgmod.load()
        except ConfigError:
            cfg = None
        raise typer.Exit(db_cmd.check(cfg))
    text = " ".join(sql) if sql else None
    raise typer.Exit(
        db_cmd.run(
            _config(), text, libraries=libraries, as_json=as_json,
            dry_run=dry_run, as_csv=as_csv, out=out, write=write, tx=tx,
            source=source, line=line, stdin_text=stdin_text, variables=variables,
            project=prj.load(proj),
        )
    )


@app.command("vscode")
def setup_vscode(
    action: Annotated[
        str,
        typer.Argument(metavar="[install|status|uninstall]",
                       help="install adds or updates; status shows installed extensions."),
    ] = "status",
    extra: Annotated[
        bool,
        typer.Option("-extra", "--extra",
                     help="Also install optional extensions (RPGLE linter, Code for IBM i)."),
    ] = False,
    only_ours: Annotated[
        bool,
        typer.Option("-only", "--only-ibmicli",
                     help="Install only the ibmicli extension."),
    ] = False,
) -> None:
    """Install the bundled VS Code extension and language support.

    Example: ibmicli vscode install

    Install VS Code, install this CLI, then run this command. The extension
    is built from bundled JavaScript without npm. Run again after updating
    ibmicli or changing tasks_key. Task edits need no reinstall.
    Provides /copy links, RPG completion, Outline, F6 tasks, debug, SQL
    execution and member comparison. Use status to inspect installation.
    """
    raise typer.Exit(
        vscode_cmd.run(action, _tasks_key(), extra=extra, only_ours=only_ours)
    )


@app.command("shell")
def start_shell() -> None:
    """Open the interactive shell (also started by ibmicli without arguments)."""
    raise typer.Exit(shell.run(app))


# Task overrides accept -var NAME=VALUE followed by more pairs,
# or repeated -var options.
VarOpt = Annotated[
    Optional[list[str]],
    typer.Option("-var", "--var", metavar="NAME=VALUE", show_default=False,
                 help="Placeholder value, e.g. -var srclib=MYAPP_SRC srcfile=QRPGLESRC; "
                      "overrides the project and path ('ibmicli tasks --guide' lists names)."),
]
VarArgs = Annotated[
    Optional[list[str]],
    typer.Argument(metavar="[NAME=VALUE...]", show_default=False,
                   help="More pairs after -var."),
]


def _given(var: list[str] | None, pairs: list[str] | None) -> dict[str, str]:
    """Parse NAME=VALUE pairs supplied through and after -var."""
    pairs = pairs or []
    if pairs and not var:
        raise InvalidName(
            f"'{pairs[0]}': placeholders require -var, e.g. -var srclib=MYAPP_SRC"
        )
    given: dict[str, str] = {}
    for text in [*(var or []), *pairs]:
        for pair in text.split():
            key, sep, value = pair.partition("=")
            if not sep or not key.strip() or not value.strip():
                raise InvalidName(f"-var '{pair}': use NAME=VALUE, e.g. srclib=MYAPP_SRC")
            given[key.strip().lower()] = value.strip()
    return given


@app.command("run")
def run_task(
    name: Annotated[
        str,
        typer.Argument(metavar="TASK", show_default=False,
                       help="Task to run; list with 'ibmicli tasks'."),
    ],
    source: Annotated[
        Optional[str],
        typer.Option("-src", "--src", metavar="FILE", show_default=False,
                     help="Local source file; omit for tasks that do not use it."),
    ] = None,
    old_member: OldMbrOpt = None,
    dry_run: Annotated[
        bool, typer.Option("-n", "--dry-run", help="Show steps without executing them."),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("-v", "--verbose",
                     help="Print the full compiler listing instead of messages only."),
    ] = False,
    var: VarOpt = None,
    pairs: VarArgs = None,
    proj: ProjOpt = None,
) -> None:
    """Run configured task steps in order, stopping at the first failure.

    Example: ibmicli run compile -src MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle -proj ibmiproj.toml -n

    Tasks run only through run NAME. -src is required only for placeholders
    that use a local source. Values come from -proj or -var NAME=VALUE;
    explicit -var values win. env=NAME also selects the environment.
    No project means no path or active-environment fallback.
    Project tasks override global tasks of the same name.

    CL steps compile the remote member. Start with a deploy step when the
    task must compile local changes.
    """
    raise typer.Exit(
        run_cmd.run(_config(), name, _source(source, old_member), dry_run=dry_run,
                    verbose=verbose, app=app, given=_given(var, pairs),
                    project=prj.load(proj))
    )


@app.command("deploy-build-changed")
def deploy_build_changed(
    since: Annotated[
        Optional[str],
        typer.Option("--since", metavar="REF", show_default=False,
                     help="Also include commits since the merge base with REF, "
                          "e.g. origin/main."),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("-n", "--dry-run", help="Show the plan and steps without executing."),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("-v", "--verbose",
                     help="Print the full compiler listing instead of messages only."),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Return only the plan as JSON without executing (VS Code)."),
    ] = False,
    proj: ProjOpt = None,
) -> None:
    """Upload and build sources changed according to Git.

    Example: ibmicli deploy-build-changed -proj ibmiproj.toml -n

    Includes differences from HEAD and untracked files below the current
    folder. --since REF also includes changes since the merge base.
    All sources upload first, then service programs build before programs.
    NOMAIN identifies service programs; this assumes one module per object.

    Configure build tasks by source file in [deploy-build-changed]. A string
    selects one task; { pgm = "buildpgm", srv = "buildsrv" } selects by NOMAIN.
    Unmapped source files upload only. Build tasks should omit deploy steps.
    --json returns the plan without executing it.
    """
    raise typer.Exit(
        buildchanged_cmd.run(
            _config(), since, dry_run=dry_run, verbose=verbose, app=app, as_json=as_json,
            project=prj.load(proj),
        )
    )


@app.command("tasks")
def show_tasks(
    name: Annotated[
        Optional[str],
        typer.Argument(metavar="[TASK]", show_default=False,
                       help="One task whose steps to show."),
    ] = None,
    ext: Annotated[
        Optional[str],
        typer.Option("-e", "--ext", metavar="EXTENSION", show_default=False,
                     help="Only tasks for this extension, e.g. sqlrpgle."),
    ] = None,
    guide: Annotated[
        bool,
        typer.Option("--guide", help="How to write a task: step types and placeholders."),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Output for the VS Code extension, one JSON line.")
    ] = False,
    proj: ProjOpt = None,
) -> None:
    """List configured tasks, or show the steps of one task.

    Example: ibmicli tasks -e sqlrpgle

    Global tasks are in config.toml; repository tasks in ibmiproj.toml are
    included with -proj. No tasks are supplied by the CLI.
    Use --guide for step types, placeholders and a complete example.
    This is the task list used by the VS Code F6 menu.
    """
    raise typer.Exit(
        tasks_cmd.run(
            _config(), name, ext=ext, as_json=as_json, guide=guide, project=prj.load(proj),
        )
    )


@app.command("env")
def env(
    action: Annotated[
        Optional[str],
        typer.Argument(metavar="[ls|set|use|refresh]", show_default=False,
                       help="Action; defaults to showing the active environment."),
    ] = None,
    name: Annotated[
        Optional[str],
        typer.Argument(metavar="[NAME]", show_default=False, help="Environment name."),
    ] = None,
    everything: Annotated[
        bool, typer.Option("-all", "--all", help="With refresh: all environments."),
    ] = False,
    test: Annotated[
        bool,
        typer.Option("--test/--no-test",
                     help="With set: test the environment on IBM i and read its library list."),
    ] = True,
    name_only: Annotated[
        bool,
        typer.Option("-q", "--name", help="Print only the active environment name."),
    ] = False,
) -> None:
    """Configure and select job library-list environments.

    Example: ibmicli env ls

    env set NAME accepts setup CL commands or explicit current library and
    user library list. The current library is the default object library.
    env refresh rereads the list after setup changes; -all refreshes all.
    env use NAME selects the environment used by db and the shell.
    Tasks and debug use the project app environment or explicit run -var.
    """
    if name_only:
        # Quiet mode for prompts: print only the active name.
        try:
            current = cfgmod.load().env()
        except ConfigError:
            current = None
        if current is None:
            raise typer.Exit(1)
        print(current.name)
        raise typer.Exit()

    what = (action or "").lower()
    if what == "":
        raise typer.Exit(env_cmd.show_active(_config()))
    if what == "ls":
        raise typer.Exit(env_cmd.list_envs(_config()))
    if what == "set":
        raise typer.Exit(env_cmd.set_env(_config(), name, test=test))
    if what == "use":
        raise typer.Exit(env_cmd.use_env(_config(), name))
    if what == "refresh":
        raise typer.Exit(env_cmd.refresh(_config(), name, everything=everything))
    output.error(f"env: '{action}' is not an action (ls, set, use, refresh)")
    raise typer.Exit(2)


@app.command("set-conn")
def set_conn(
    force: Annotated[
        bool, typer.Option("-f", "--force", help="Replace connection settings without confirmation.")
    ] = False,
    test: Annotated[
        bool, typer.Option("--test/--no-test", help="Test the connection before saving.")
    ] = True,
) -> None:
    """Prompt for connection settings and save them in global configuration."""
    raise typer.Exit(setconn_cmd.run(force=force, test=test))


@app.command("set-project")
def set_project(
    name: Annotated[
        str,
        typer.Argument(metavar="APP", show_default=False,
                       help="App folder below the repository root, e.g. MYAPP."),
    ],
    proj: ProjOpt = None,
    env_name: Annotated[
        Optional[str],
        typer.Option("-env", "--env", metavar="NAME", show_default=False,
                     help="App environment; '-' means none."),
    ] = None,
    objlib: Annotated[
        Optional[str],
        typer.Option("-objlib", "--objlib", metavar="LIBRARY", show_default=False,
                     help="Object library; '-' uses the environment current library."),
    ] = None,
    srclib: Annotated[
        Optional[str],
        typer.Option("-srclib", "--srclib", metavar="LIBRARY", show_default=False,
                     help="Single source library: source files sit directly in the app "
                          "folder (APP/QRPGLESRC/...). '-' goes back to library folders."),
    ] = None,
) -> None:
    """Add or update an app in the repository ibmiproj.toml.

    Example: ibmicli set-project MYAPP -env dev -objlib MYAPP_OBJ -proj ibmiproj.toml

    Creates the project file if missing. An app is a folder below its root:
    MYAPP/MYAPP_SRC/QRPGLESRC/HELLO.sqlrpgle. The path supplies source library
    and source file. env selects the global environment; objlib overrides
    its current library. Apps without an environment support source reads.
    With -srclib the app has no library folders: MYAPP/QRPGLESRC/HELLO.sqlrpgle
    is member MYAPP_SRC/QRPGLESRC(HELLO) when srclib is MYAPP_SRC.
    Missing -env and -objlib values are prompted for. '-' clears a value.
    VS Code installation is handled by vscode install.
    """
    raise typer.Exit(
        setproject_cmd.run(_config(), name, proj, env=env_name, objlib=objlib, srclib=srclib)
    )


@app.command("config")
def show_config(
    short: Annotated[
        bool,
        typer.Option("-s", "--short", help="Summary only, without full environment and task details."),
    ] = False,
) -> None:
    """Show effective global configuration without the password.

    Example: ibmicli config -s

    Includes default extension mappings and encoding values, even if absent
    from the file. Tasks exist only when configured. -s shows a summary.
    """
    raise typer.Exit(config_cmd.run(_config(), short=short))


def main() -> None:
    app()


__all__ = ["app", "main"]
