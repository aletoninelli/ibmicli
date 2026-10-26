"""Display a source member on stdout or in a read-only temporary editor file."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from .. import output
from .. import session as sessions
from .. import vscode
from ..config import Config
from ..naming import InvalidName, parse_target
from ..session import Session, SessionError

EDITORS = ("nano", "vsc")
Source = tuple[str, str, str, list[str]]


def run(cfg: Config, targets: list[str], editor: str | None = None) -> int:
    """Number of unreadable members."""
    if editor:
        command = _editor_command(editor)
        if command is None:
            return 1
        return _edit(cfg, targets, editor, command)

    failures = 0
    session = sessions.acquire(cfg)
    try:
        for target in targets:
            if not _show(session, target):
                failures += 1
    finally:
        sessions.release(session)
    return failures


def _show(session: Session, target: str) -> bool:
    source = _read(session, target)
    if source is None:
        return False

    for line in source[3]:
        print(line)
    return True


def _read(session: Session, target: str) -> Source | None:
    try:
        library, srcfile, member = parse_target(target)
    except InvalidName as exc:
        output.error(str(exc))
        return None

    try:
        lines = session.get(library, srcfile, member)
    except SessionError as exc:
        output.fail(f"{library}/{srcfile},{member}")
        output.note(str(exc))
        return None

    return library, srcfile, member, lines


def _editor_command(editor: str) -> list[str] | None:
    if editor == "nano":
        executable = shutil.which("nano")
        options = []
    else:
        # Use the same code lookup as 'ibmicli vscode':
        # the executable may not be in PATH.
        found = vscode.find_code()
        executable = str(found) if found else None
        options = ["--wait"]

    if executable is None:
        output.fail(f"editor {editor} not found")
        if editor == "vsc":
            output.note("install Visual Studio Code or the 'code' command")
        return None
    return [executable, *options]


def _edit(cfg: Config, targets: list[str], editor: str, command: list[str]) -> int:
    failures = 0
    sources: list[Source] = []
    session = sessions.acquire(cfg)
    try:
        for target in targets:
            source = _read(session, target)
            if source is None:
                failures += 1
            else:
                sources.append(source)
    finally:
        sessions.release(session)

    if not sources:
        return failures

    with TemporaryDirectory(prefix="ibmicli-show-") as directory:
        paths = [_temporary_source(cfg, Path(directory), source) for source in sources]
        try:
            result = subprocess.run([*command, *(str(path) for path in paths)], check=False)
        except OSError as exc:
            output.fail(f"could not open {editor}")
            output.note(str(exc))
            return failures + 1
        if result.returncode:
            output.fail(f"{editor} exited with status {result.returncode}")
            failures += 1
    return failures


def _temporary_source(cfg: Config, root: Path, source: Source) -> Path:
    library, srcfile, member, lines = source
    ext = cfg.ext_for_srcfile(srcfile)
    name = f"{member}.{ext}" if ext else member
    path = root / library / srcfile / name
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + ("\n" if lines else "")
    path.write_text(text, encoding=cfg.local_charset)
    # Make temporary source copies read-only because show is for inspection.
    path.chmod(0o444)
    return path
