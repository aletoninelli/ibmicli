"""List source members as LIBRARY/FILE,MEMBER on stdout.

Explicit options or the current project source-file folder supply names.
Diagnostics go to stderr so member lines can be passed to another command.
"""

from __future__ import annotations

from pathlib import Path

from .. import output, project as prj
from .. import session as sessions
from ..config import Config, ConfigError
from ..session import Session, SessionError

# -f value for all source files in a library.
ALL = "*ALL"


def run(
    cfg: Config,
    library: str | None = None,
    srcfile: str | None = None,
    project: prj.Project | None = None,
) -> int:
    """Return 0 for a completed list, 1 for failure."""
    lib, srcf = _where(cfg, project, library, srcfile)  # ConfigError is handled by cli.Commands.
    session = sessions.acquire(cfg)
    try:
        if srcf == ALL:
            return _whole_library(session, lib)
        return _one_file(session, lib, srcf)
    finally:
        sessions.release(session)


def _where(
    cfg: Config, project: prj.Project | None, library: str | None, srcfile: str | None
) -> tuple[str, str]:
    """Explicit library/source file, or resolve the current project folder."""
    lib = (library or "").strip().upper()
    srcf = (srcfile or "").strip().upper()
    if lib and srcf:
        return lib, srcf

    found = prj.resolve(cfg, project, Path.cwd())
    if found is not None:
        lib = lib or prj.source_library(found)
        srcf = srcf or found.srcfile
    elif project is not None and not lib:
        lib = prj.library_at(project, Path.cwd())
    missing = [
        label
        for label, value in (("library (-lib)", lib), ("source file (-f)", srcf))
        if not value
    ]
    if missing:
        where = ("from a project source-file folder" if project is not None
                 else f"with -proj ROOT/{prj.PROJECT_FILE} from a source-file folder")
        raise ConfigError(
            f"missing {', '.join(missing)}: supply it as an option, or "
            f"run {where}"
        )
    return lib, srcf


def _one_file(session: Session, library: str, srcfile: str) -> int:
    try:
        members = session.members(library, srcfile)
    except SessionError as exc:
        output.fail(f"{library}/{srcfile}  (could not list members)")
        output.note(str(exc))
        return 1
    if not members:
        output.skip(f"{library}/{srcfile}  (no members)")
        return 0
    for member in members:
        print(f"{library}/{srcfile},{member}")
    return 0


def _whole_library(session: Session, library: str) -> int:
    """All members of every file in a library."""
    try:
        srcfiles = session.srcfiles(library)
    except SessionError as exc:
        output.fail(f"{library}  (could not list files)")
        output.note(str(exc))
        return 1
    if not srcfiles:
        output.skip(f"{library}  (no files)")
        return 0

    # An unreadable file must not block other source files.
    # Report it on stderr while keeping stdout usable.
    failures = 0
    for srcfile in srcfiles:
        try:
            members = session.members(library, srcfile)
        except SessionError as exc:
            output.skip(f"{library}/{srcfile}  ({exc})")
            failures += 1
            continue
        for member in members:
            print(f"{library}/{srcfile},{member}")
    return 1 if failures and failures == len(srcfiles) else 0
