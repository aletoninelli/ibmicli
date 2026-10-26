"""Download IBM i members to local files."""

from __future__ import annotations

from pathlib import Path

from .. import output
from .. import session as sessions
from ..config import Config
from ..naming import InvalidName, split_member_arg
from ..session import Session, SessionError


def run(
    cfg: Config,
    library: str,
    srcfile: str,
    members: list[str],
    dest_dir: Path,
    dry_run: bool = False,
    force: bool = False,
) -> int:
    """Number of members not downloaded; an empty members list means all."""
    failures = 0
    session = sessions.acquire(cfg)
    try:
        wanted = _resolve(cfg, session, library, srcfile, members)
        if wanted is None:
            return 1
        for member, ext in wanted:
            dest = dest_dir / f"{member}.{ext}" if ext else dest_dir / member
            if not _fetch(cfg, session, library, srcfile, member, dest, dry_run, force):
                failures += 1
    finally:
        sessions.release(session)
    return failures


def _resolve(
    cfg: Config, session: Session, library: str, srcfile: str, members: list[str]
) -> list[tuple[str, str]] | None:
    """Members to download as name and local extension."""
    if members:
        resolved = []
        for arg in members:
            try:
                name, ext = split_member_arg(arg)
            except InvalidName as exc:
                output.skip(f"{arg}  ({exc})")
                continue
            resolved.append((name, ext or cfg.ext_for_srcfile(srcfile)))
        return resolved

    # No member filter: include the whole source file.
    try:
        names = session.members(library, srcfile)
    except SessionError as exc:
        output.fail(f"{library}/{srcfile}  (could not list members)")
        output.note(str(exc))
        return None
    if not names:
        output.skip(f"{library}/{srcfile}  (no members found)")
        return None

    ext = cfg.ext_for_srcfile(srcfile)
    return [(name, ext) for name in names]


def _fetch(
    cfg: Config,
    session: Session,
    library: str,
    srcfile: str,
    member: str,
    dest: Path,
    dry_run: bool,
    force: bool,
) -> bool:
    remote = f"{library}/{srcfile}({member})"

    if dry_run:
        output.dry(f"{remote}  ->  {dest}")
        return True
    if dest.exists() and not force:
        output.skip(f"{dest}  (already exists; use -f to overwrite)")
        return False

    try:
        lines = session.get(library, srcfile, member)
    except SessionError as exc:
        output.fail(f"{remote}  ->  {dest}")
        output.note(str(exc))
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines) + "\n" if lines else "", encoding=cfg.local_charset)
    output.ok(f"{remote}  ->  {dest}  ({len(lines)} lines)")
    return True
