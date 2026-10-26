"""Resolve /copy directives to local source files without transferring them.

The explicit project maps libraries to app folders and source files to
subfolders. Unqualified copies follow the app environment library list.
Resolved values let commands/copy.py download missing members through pull.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .naming import InvalidName, check_member, check_object
from .project import Project
from . import project as prj

# /copy and /include can appear at any column, with or without a library:
# /copy MYAPP_SRC/QPROTSRC,PROCS     /copy QCPYSRC,VARIABLES
# /include VENDORLIB/QRPGLESRC,HELPERS
# Parenthesized member syntax is also accepted.
# An initial asterisk marks a fixed-format comment in column 7;
# commented lines must not become directives.
DIRECTIVE = re.compile(
    r"""
    (?P<indent>^\s*)                      # leading whitespace
    (?P<keyword>/(?:copy|include))        # /copy or /include
    \s+
    (?P<spec>
        (?:[A-Za-z0-9$#@_.*]+/)?          # optional library
        [A-Za-z0-9$#@_.*]+                # source file
        (?:,[A-Za-z0-9$#@_.]+|\([A-Za-z0-9$#@_.]+\))   # ,MEMBER or (MEMBER)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


class CopyError(Exception):
    """A directive with no resolvable local destination.

    This differs from ConfigError: a valid configuration can still leave
    the source outside any declared app. Report how to supply its location.
    """


@dataclass(frozen=True)
class Spec:
    """Parsed directive: optional library, source file and member."""

    library: str  # Empty when omitted from the directive.
    srcfile: str
    member: str
    line: int = 0  # One-based source line; 0 for a standalone spec.
    start: int = 0  # Zero-based start column of the spec.
    end: int = 0

    @property
    def text(self) -> str:
        head = f"{self.library}/" if self.library else ""
        return f"{head}{self.srcfile},{self.member}"


@dataclass
class Resolved:
    """Local or prospective copy path and its remote names."""

    spec: Spec
    library: str  # Explicit or resolved through the library list.
    srcfile: str
    member: str
    path: Path  # Local path, whether it exists or not.
    exists: bool

    @property
    def remote(self) -> str:
        """Member in list and pull -path format."""
        return f"{self.library}/{self.srcfile},{self.member}"


def parse(text: str) -> Spec | None:
    """Parse a copy spec or complete source directive into Spec."""
    found = DIRECTIVE.search(text)
    raw = found.group("spec") if found else text.strip()
    if not raw:
        raise InvalidName("supply a copy as LIBRARY/SOURCEFILE,MEMBER")
    return _spec(raw)


def _spec(raw: str) -> Spec:
    if raw.endswith(")") and "(" in raw:
        head, _, member = raw[:-1].partition("(")
    else:
        head, sep, member = raw.partition(",")
        if not sep:
            raise InvalidName(f"'{raw}': missing member; use SOURCEFILE,MEMBER")

    library, sep, srcfile = head.partition("/")
    if not sep:
        library, srcfile = "", library

    return Spec(
        library=check_object(library, "library") if library else "",
        srcfile=check_object(srcfile, "source file"),
        member=check_member(Path(member.strip()).stem or member),
    )


def scan(text: str) -> list[Spec]:
    """All valid directives with source lines and columns.

    Invalid directives do not hide valid links elsewhere in the source.
    Columns let VS Code underline only the path.
    """
    found: list[Spec] = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = DIRECTIVE.search(line)
        if not match:
            continue
        try:
            spec = _spec(match.group("spec"))
        except InvalidName:
            continue
        found.append(
            Spec(
                library=spec.library,
                srcfile=spec.srcfile,
                member=spec.member,
                line=number,
                start=match.start("spec"),
                end=match.end("spec"),
            )
        )
    return found


def resolve(cfg: Config, project: Project | None, spec: Spec, origin: Path) -> Resolved:
    """Resolve a directive relative to its source and explicit project.

    The result can name a missing local file for a later pull. CopyError
    means even the destination folder cannot be determined.
    """
    if project is None:
        raise CopyError(
            f"'{spec.text}': source locations require -proj\n"
            f"       supply the project with -proj ROOT/{prj.PROJECT_FILE}"
        )
    source = origin.resolve()
    if spec.library:
        # With an explicit library, the origin is needed only as the fallback
        # app when no declared app already has that library.
        library = spec.library
        path = _member_file(cfg, _folder(project, library, spec.srcfile, source),
                            spec.member, spec.srcfile)
    else:
        library, path = _from_library_list(cfg, project, source, spec)
    return Resolved(
        spec=spec,
        library=library,
        srcfile=spec.srcfile,
        member=spec.member,
        path=path,
        exists=path.is_file(),
    )


def _from_library_list(
    cfg: Config, project: Project, source: Path, spec: Spec
) -> tuple[str, Path]:
    """Search unqualified copies in current-library then user-list order.

    Use the first existing local member. If absent from the whole list,
    fall back to the source library folder as the download destination.
    """
    found = prj.resolve(cfg, project, source)
    if found is None:
        raise CopyError(
            f"'{spec.text}' omits the library and {source.name} is outside "
            f"APP/LIBRARY/SOURCEFILE in {project.path}\n"
            f"       supply the library in the directive, "
            f"or move the source into its app folder"
        )
    if found.env is not None:
        for library in found.env.search:
            folder = prj.folder_for(project, library, spec.srcfile, source)
            if folder is None:
                continue
            path = _member_file(cfg, folder, spec.member, spec.srcfile)
            if path.is_file():
                return library, path

    library = prj.source_library(found)
    return library, _member_file(cfg, _folder(project, library, spec.srcfile, source),
                                 spec.member, spec.srcfile)


def _folder(project: Project, library: str, srcfile: str, source: Path) -> Path:
    """Local LIBRARY/SRCFILE folder across declared apps.

    If no app has this library, place it beside the source in its app.
    """
    mapped = prj.folder_for(project, library, srcfile, source)
    if mapped is not None:
        return mapped
    app = project.app_of(source)
    placed = prj.place(project, app, library, srcfile) if app is not None else None
    if placed is not None:
        return placed
    if app is not None and app.srclib:
        # A two-level app holds its own library only: other libraries need
        # an app of their own.
        raise CopyError(
            f"library {library} is absent from all apps in {project.path}\n"
            f"       app {app.name} holds only {app.srclib} (srclib): "
            f"add an app for {library} with 'ibmicli set-project'"
        )
    raise CopyError(
        f"library {library} is absent from all apps in {project.path}\n"
        f"       {source.name} is outside a declared app to download into: "
        f"add its app with 'ibmicli set-project'"
    )


def _member_file(cfg: Config, folder: Path, member: str, srcfile: str) -> Path:
    """Find the existing local member, ignoring case and extension.

    IBM i member names are uppercase and have no extension. Prefer existing
    on-disk spelling before constructing the name a pull would create.
    """
    entry = _listing(folder).get(member.upper())
    if entry is not None:
        return entry

    ext = cfg.ext_for_srcfile(srcfile)
    return folder / (f"{member}.{ext}" if ext else member)


# Cache folder listings until they change. Unqualified copies search
# multiple libraries, repeated for every directive in a source during
# completion and copy -all.
_listings: dict[Path, tuple[int, dict[str, Path]]] = {}


def _listing(folder: Path) -> dict[str, Path]:
    """Uppercase member name without extension -> local file in folder."""
    try:
        stamp = folder.stat().st_mtime_ns
    except OSError:
        return {}
    cached = _listings.get(folder)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    found: dict[str, Path] = {}
    try:
        for entry in sorted(folder.iterdir()):
            if entry.is_file():
                found.setdefault(entry.stem.upper(), entry)
    except OSError:
        return {}
    _listings[folder] = (stamp, found)
    return found
