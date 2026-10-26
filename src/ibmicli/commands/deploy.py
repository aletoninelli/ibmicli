"""Upload local files to IBM i source members."""

from __future__ import annotations

from pathlib import Path

from .. import output
from .. import session as sessions
from ..config import Config
from ..naming import InvalidName, split_file
from ..session import Session, SessionError, read_local


def run(
    cfg: Config,
    library: str,
    srcfile: str,
    files: list[Path],
    dry_run: bool = False,
    srctype: str | None = None,
) -> int:
    """Number of files not transferred."""
    failures = 0
    session = sessions.acquire(cfg)
    try:
        for target in files:
            if not _deploy_one(cfg, session, library, srcfile, target, dry_run, srctype):
                failures += 1
    finally:
        sessions.release(session)
    return failures


def _deploy_one(
    cfg: Config,
    session: Session,
    library: str,
    srcfile: str,
    target: Path,
    dry_run: bool,
    srctype_override: str | None,
) -> bool:
    if not target.is_file():
        output.skip(f"{target}  (file not found)")
        return False

    try:
        member, ext = split_file(target)
    except InvalidName as exc:
        output.skip(f"{target}  ({exc})")
        return False

    srctype = ""
    if srctype_override:
        srctype = srctype_override.upper()
    elif cfg.set_srctype and ext:
        srctype = cfg.srctype_for(ext)

    remote = f"{library}/{srcfile}({member})"
    desc = remote + (f"  type {srctype}" if srctype else "")

    if dry_run:
        output.dry(f"{target}  ->  {desc}")
        return True

    try:
        data = read_local(target, cfg)
        session.put(library, srcfile, member, data)
    except SessionError as exc:
        output.fail(f"{target}  ->  {desc}")
        output.note(str(exc))
        return False

    if srctype:
        _set_srctype(session, library, srcfile, member, srctype, target)
    output.ok(f"{target}  ->  {desc}")
    return True


def _set_srctype(
    session: Session, library: str, srcfile: str, member: str, srctype: str, target: Path
) -> None:
    """Set SRCTYPE after upload, including newly created members.

    Missing SRCTYPE appears as *BLANK in RDI. Failure here does not invalidate
    an otherwise successful upload.
    """
    try:
        session.rcmd(f"CHGPFM FILE({library}/{srcfile}) MBR({member}) SRCTYPE({srctype})")
    except SessionError as exc:
        output.note(f"{target} uploaded but SRCTYPE({srctype}) was not set: {exc}")
