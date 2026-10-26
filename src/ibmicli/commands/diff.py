"""Compare local sources with IBM i members in memory.

Diff direction matches deploy: remote lines are '-' and local lines '+'.
Normalize trailing blanks and empty final lines. Reencode local text as
deploy would, replacing unrepresentable characters with '?', so equality
means an upload would not change the member.

status lists members once per source-file folder and reads each local file's
remote counterpart. Neither comparison writes local files.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .. import output, project as prj
from .. import session as sessions
from ..config import Config, ConfigError
from ..naming import InvalidName, check_object, split_file
from ..session import Session, SessionError


@dataclass
class Pair:
    """Local file and the member to compare."""

    path: Path
    library: str
    srcfile: str
    member: str

    @property
    def remote(self) -> str:
        return f"{self.library}/{self.srcfile}({self.member})"


@dataclass
class Counts:
    same: int = 0
    different: int = 0
    only_local: int = 0
    only_remote: int = 0
    failed: int = 0
    skipped: list[str] = field(default_factory=list)


# --- Diff ------------------------------------------------------------------


def run_diff(
    cfg: Config,
    files: list[Path],
    library: str | None = None,
    srcfile: str | None = None,
    as_json: bool = False,
    dry_run: bool = False,
    context: int = 3,
    project: prj.Project | None = None,
) -> int:
    """Return 0 for all equal, 1 for any difference or read failure."""
    pairs = [pair(cfg, project, path, library, srcfile) for path in files]

    if dry_run:
        for item in pairs:
            output.dry(f"{item.path}  <->  {item.remote}")
        return 0

    outcome = 0
    session = sessions.acquire(cfg)
    try:
        for item in pairs:
            if not _diff_one(cfg, session, item, as_json, context):
                outcome = 1
    finally:
        sessions.release(session)
    return outcome


def pair(
    cfg: Config, project: prj.Project | None, path: Path, library: str | None, srcfile: str | None
) -> Pair:
    """Resolve remote names from explicit options or the project, as for deploy."""
    member, _ = split_file(path)
    if library and srcfile:
        return Pair(path, check_object(library, "library"), check_object(srcfile, "source file"), member)
    found = prj.resolve(cfg, project, path)
    if found is None:
        if project is None:
            raise prj.no_project(f"{path}: supply -lib LIBRARY -file SRCFILE")
        raise ConfigError(
            f"{path} is outside APP/LIBRARY/SOURCEFILE in {project.path}\n"
            "       supply -lib and -file"
        )
    lib = check_object(library or prj.source_library(found), "library")
    return Pair(path, lib, check_object(srcfile or found.srcfile, "source file"), member)


def _diff_one(cfg: Config, session: Session, item: Pair, as_json: bool, context: int) -> bool:
    """Whether the file matches the member."""
    if not item.path.is_file():
        if as_json:
            _json(item, error="local file not found")
        else:
            output.skip(f"{item.path}  (file not found)")
        return False
    try:
        remote = normalized(session.get(item.library, item.srcfile, item.member))
    except SessionError as exc:
        if as_json:
            _json(item, error=str(exc).splitlines()[0] if str(exc) else "read failed")
        else:
            output.fail(f"{item.path}  <->  {item.remote}")
            output.note(str(exc))
        return False

    local = local_lines(cfg, item.path)
    same = local == remote
    if as_json:
        _json(item, same=same, text="\n".join(remote) + ("\n" if remote else ""))
        return same
    if same:
        output.ok(f"{item.path}  =  {item.remote}  (equal)")
        return True
    for line in difflib.unified_diff(
        remote, local,
        fromfile=f"{item.remote}  (IBM i)", tofile=f"{item.path}  (local)",
        lineterm="", n=context,
    ):
        output.diff(line)
    return False


def _json(item: Pair, **fields: object) -> None:
    print(json.dumps(
        {"file": str(item.path.resolve()), "remote": item.remote, **fields},
        ensure_ascii=False,
    ))


def normalized(lines: list[str]) -> list[str]:
    """Remove record padding and empty final lines before comparing."""
    result = [line.rstrip() for line in lines]
    while result and not result[-1]:
        result.pop()
    return result


def local_lines(cfg: Config, path: Path) -> list[str]:
    """Local source as it would arrive after deploy encoding."""
    text = path.read_text(encoding=cfg.local_charset, errors="replace")
    text = text.encode(cfg.remote_charset, errors="replace").decode(cfg.remote_charset)
    return normalized(text.splitlines())


def changes(remote: list[str], local: list[str]) -> tuple[int, int]:
    """Number of lines an upload would add and remove."""
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, remote, local, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


# --- Status ----------------------------------------------------------------


def run_status(
    cfg: Config,
    start: Path,
    remote_too: bool = False,
    verbose: bool = False,
    dry_run: bool = False,
    project: prj.Project | None = None,
) -> int:
    """Return 0 for equal files, 1 for differences, missing members or read failures."""
    folders = _folders(cfg, project, start)
    if not folders:
        raise ConfigError(
            f"no source-file folders to compare in {start}"
        )

    counts = Counts()
    session = None if dry_run else sessions.acquire(cfg)
    try:
        for folder, target in folders:
            _status_folder(cfg, session, folder, target, counts, remote_too, verbose)
    finally:
        output.progress_end()
        if session is not None:
            sessions.release(session)

    if dry_run:
        return 0
    _summary(counts, remote_too)
    trouble = counts.different + counts.only_local + counts.failed
    return 1 if trouble else 0


def _folders(
    cfg: Config, project: prj.Project | None, start: Path
) -> list[tuple[Path, prj.Target]]:
    """Known source-file folders at or below start in declared apps.

    Library folders include their source files; app folders include all their
    libraries. Other folders are ignored. Report unavailable app
    environments once and continue with the other apps.
    """
    if project is None:
        raise ConfigError(
            f"status compares app folders: supply -proj ROOT/{prj.PROJECT_FILE}\n"
            "       for a single file: ibmicli diff FILE -lib LIBRARY -file SRCFILE"
        )
    start = start.resolve()
    here = project.app_of(start)
    if here is not None:
        apps = [(here, start.relative_to(here.root.resolve()).parts)]
    else:
        apps = [(app, ()) for app in project.apps.values()
                if app.root.resolve() == start or start in app.root.resolve().parents]
        if not apps:
            raise ConfigError(
                f"{start} is outside the apps in {project.path}: status compares "
                "source-file folders\n"
                "       for a single file: ibmicli diff FILE -lib LIBRARY -file SRCFILE"
            )

    known = cfg.known_srcfiles
    found: list[tuple[Path, prj.Target]] = []
    for app, parts in apps:
        if not app.root.is_dir():
            continue  # App declared in TOML but not yet present locally.
        try:
            for folder in _srcfile_folders(app, parts):
                try:
                    target = prj.resolve(cfg, project, folder)
                except InvalidName:
                    continue  # templates_html is not an IBM i object name.
                if target is not None and target.srcfile in known:
                    found.append((folder, target))
        except ConfigError as exc:
            # The missing app environment applies to all its folders;
            # report it only once.
            output.skip(f"{_shown(app.root)}  ({str(exc).splitlines()[0]})")
    return found


def _srcfile_folders(app: prj.App, parts: tuple[str, ...]) -> list[Path]:
    """Candidate source-file folders of app at or below parts.

    A srclib app keeps them directly in the app folder; otherwise they sit
    one level down, inside library folders.
    """
    if app.srclib:
        return [app.root / parts[0]] if parts else _subfolders(app.root)
    libraries = [app.root / parts[0]] if parts else _subfolders(app.root)
    folders: list[Path] = []
    for library in libraries:
        if library.is_dir():
            folders += [library / parts[1]] if len(parts) > 1 else _subfolders(library)
    return folders


def _subfolders(folder: Path) -> list[Path]:
    return sorted(
        entry for entry in folder.iterdir()
        if entry.is_dir() and not entry.name.startswith(".")
    )


def _status_folder(
    cfg: Config,
    session: Session | None,
    folder: Path,
    target: prj.Target,
    counts: Counts,
    remote_too: bool,
    verbose: bool,
) -> None:
    library = prj.source_library(target)
    srcfile = target.srcfile
    files: dict[str, Path] = {}
    for path in sorted(entry for entry in folder.iterdir()
                       if entry.is_file() and not entry.name.startswith(".")):
        try:
            member, _ = split_file(path)
        except InvalidName as exc:
            counts.skipped.append(f"{_shown(path)}  ({exc})")
            continue
        files.setdefault(member, path)

    if session is None:
        output.dry(f"{_shown(folder)}  <->  {library}/{srcfile}  ({len(files)} files)")
        return

    try:
        members = set(session.members(library, srcfile))
    except SessionError as exc:
        output.progress_end()
        output.fail(f"{_shown(folder)}  <->  {library}/{srcfile}  (could not list members)")
        output.note(str(exc))
        counts.failed += len(files)
        return

    for number, (member, path) in enumerate(files.items(), start=1):
        remote = f"{library}/{srcfile}({member})"
        output.progress(f"[{number}/{len(files)}] {_shown(path)}")
        if member not in members:
            counts.only_local += 1
            output.progress_end()
            output.tagged("LOCAL", f"{_shown(path)}  (missing on IBM i: {remote})")
            continue
        try:
            theirs = normalized(session.get(library, srcfile, member))
        except SessionError as exc:
            counts.failed += 1
            output.progress_end()
            output.fail(f"{_shown(path)}  <->  {remote}")
            output.note(str(exc))
            continue
        ours = local_lines(cfg, path)
        if ours == theirs:
            counts.same += 1
            if verbose:
                output.progress_end()
                output.ok(f"{_shown(path)}  =  {remote}")
            continue
        counts.different += 1
        added, removed = changes(theirs, ours)
        output.progress_end()
        output.tagged("DIFF", f"{_shown(path)}  ->  {remote}  (+{added} -{removed})")

    only_remote = sorted(members - set(files))
    counts.only_remote += len(only_remote)
    if remote_too:
        output.progress_end()
        for member in only_remote:
            output.tagged("IBMI", f"{library}/{srcfile}({member})  (missing locally)")


def _summary(counts: Counts, remote_too: bool) -> None:
    for line in counts.skipped:
        output.skip(line)
    total = counts.same + counts.different + counts.only_local + counts.failed
    parts = [f"{counts.same} equal", f"{counts.different} different",
             f"{counts.only_local} local only"]
    if counts.failed:
        parts.append(f"{counts.failed} unreadable")
    text = f"{total} files compared: " + ", ".join(parts)
    if counts.only_remote:
        text += f"; {counts.only_remote} remote-only members"
        if not remote_too:
            text += " (-remote to list them)"
    output.info("")
    output.info(text)
    if counts.different:
        output.info("show one file diff: ibmicli diff FILE")


def _shown(path: Path) -> str:
    """Path relative to the current folder when possible."""
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)
